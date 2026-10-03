"""Generate a brew recipe from a handful of parameters — the logic, with no UI.

This module is the **logical API** behind the recipe generator. It knows how to turn a
small set of parameters (dose, ratio, temperature, pours…) into a complete, validated
recipe; where to cache the result; and how to hand one cached file to ``xbloom brew``.
It never prints, never prompts, and never imports a UI toolkit — the terminal guide
(``scripts/gen-brew.py``) and the local web UI (:mod:`xbloom_ble.webui`) are both thin
front-ends over what is here, so they cannot drift from each other.

The shape of a generated brew is fixed: a bloom plus 2-3 main pours, temperature
stepping down 1 C per pour, equal main-pour volumes, no vibration on any pour.

Safety (see CLAUDE.md's invariant): :func:`brew_argv` builds a **load-only** command
unless ``start=True`` is passed explicitly, and :func:`run_brew` refuses a remote start
unless the caller states that a human is present (``attended=True``). Establishing that
is each front-end's job — a TTY prompt in the terminal guide, a typed confirmation in
the web UI — because only the front-end can tell whether anyone answered.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

# --- the fixed parts of the shape -------------------------------------------
# Bloom volume is a multiple of the dose: 3x at 10 g and below, sliding linearly to
# 2x at 15 g and above (a big dose does not want a proportionally huge bloom).
BLOOM_MULT_LO, DOSE_LO = 3.0, 10.0
BLOOM_MULT_HI, DOSE_HI = 2.0, 15.0

# The largest single pour the phone app ever sends. The protocol layer can split a
# bigger one, but that frame shape has never been seen on hardware.
APP_MAX_POUR_ML = 127


class ParamError(ValueError):
    """A parameter value was the wrong type, or outside its allowed choices."""


def bloom_multiplier(dose: float) -> float:
    if dose <= DOSE_LO:
        return BLOOM_MULT_LO
    if dose >= DOSE_HI:
        return BLOOM_MULT_HI
    span = (dose - DOSE_LO) / (DOSE_HI - DOSE_LO)
    return BLOOM_MULT_LO + span * (BLOOM_MULT_HI - BLOOM_MULT_LO)


def volumes(dose: float, ratio: float, n_main: int) -> tuple[int, int, int]:
    """→ (bloom_ml, main_ml, total_ml).

    The machine rejects a load whose ratio byte disagrees with Σ(pour ml) / dose, and
    "the rest of the pours are equal volume" has to hold exactly, so the two hard
    constraints are ``main * n + bloom == total`` with a single integer ``main``. The
    bloom absorbs the rounding slack — it is a target from a formula, whereas the
    other two are checked by the machine and by the brief.
    """
    total = round(dose * ratio)
    main = round((total - round(dose * bloom_multiplier(dose))) / n_main)
    return total - main * n_main, main, total


# --- parameters: ONE table drives argparse, the guide AND the web UI ---------
@dataclass(frozen=True)
class Param:
    key: str                      # argparse dest / BrewParams field name
    flag: str
    label: str
    type: Any
    default: Any
    hint: str
    unit: str = ""
    choices: tuple | None = None
    # Advisory bounds for a web UI's number inputs. They are NOT the authority on what
    # is legal — Recipe.validate() is — so a value outside them still round-trips and
    # simply shows up as invalid, rather than being silently clamped.
    lo: float | None = None
    hi: float | None = None
    step: float | None = None

    def show(self, value: Any) -> str:
        """The value as a human reads it: ``1:17``, ``91 C``, ``3.5 ml/s``."""
        if self.key == "ratio":
            return f"1:{value:g}"
        return f"{value:g}{self.unit}" if isinstance(value, float) else f"{value}{self.unit}"

    def coerce(self, raw: Any) -> Any:
        """Parse one untrusted value (a CLI string, a JSON field) → ParamError."""
        try:
            value = self.type(raw)
        except (TypeError, ValueError) as exc:
            raise ParamError(f"{self.label}: '{raw}' is not a {self.type.__name__}") from exc
        if self.choices and value not in self.choices:
            allowed = ", ".join(str(c) for c in self.choices)
            raise ParamError(f"{self.label} must be one of {allowed}")
        return value


PARAMS: tuple[Param, ...] = (
    Param("dose", "--dose", "dose", float, 10.0, "1-18 g", " g", lo=1, hi=18, step=0.5),
    Param("ratio", "--ratio", "ratio", float, 17.0, "water:coffee", lo=10, hi=22, step=0.5),
    Param("temp", "--temp", "temp", int, 91, "first pour; -1 C each pour after", " C",
          lo=60, hi=99, step=1),
    Param("bloom_time", "--bloom-time", "bloom time", int, 50, "pause after the bloom", " s",
          lo=0, hi=180, step=5),
    Param("pattern", "--pattern", "pattern", str, "spiral",
          "dominant: bloom + all main pours but the first", choices=("spiral", "center")),
    Param("pours", "--pours", "main pours", int, 3, "2 or 3 (plus the bloom)",
          choices=(2, 3)),
    Param("pause", "--pause", "pause", int, 20, "between main pours", " s",
          lo=0, hi=120, step=5),
    Param("rpm", "--rpm", "rpm", int, 60, "holder speed on spiral pours; center uses 0",
          lo=0, hi=120, step=5),
    Param("flow", "--flow", "flow", float, 3.5, "3.0-3.5", " ml/s", lo=1.5, hi=5.0, step=0.1),
    Param("grind", "--grind", "grind", int, 0, "0 = pre-ground, else 1-80", lo=0, hi=80, step=1),
)

PARAMS_BY_KEY: dict[str, Param] = {p.key: p for p in PARAMS}


@dataclass
class BrewParams:
    """The complete parameter state of a generated brew.

    Field names match :data:`PARAMS` keys one-for-one (``test_brewgen`` asserts it), so
    an argparse ``Namespace`` and a JSON body can both be funnelled through
    :meth:`from_mapping` without a translation table in between.
    """

    dose: float = 10.0
    ratio: float = 17.0
    temp: int = 91
    bloom_time: int = 50
    pattern: str = "spiral"
    pours: int = 3
    pause: int = 20
    rpm: int = 60
    flow: float = 3.5
    grind: int = 0
    # Not a Param: it has no default to compare against and no place in the table.
    name: str | None = None

    @classmethod
    def from_mapping(cls, data: Any, *, base: BrewParams | None = None) -> BrewParams:
        """Build from untrusted keys/values, coercing each through its :class:`Param`.

        Missing keys fall back to ``base`` (or the defaults), so a front-end can send a
        partial update. Unknown keys are an error rather than being ignored — a typo'd
        ``--tempurature`` that silently brewed at the default is exactly the kind of
        thing worth failing loudly on.
        """
        out = replace(base) if base is not None else cls()
        if not isinstance(data, dict):
            raise ParamError("expected an object of parameters")
        for key, raw in data.items():
            if key == "name":
                out.name = None if raw in (None, "") else str(raw)
                continue
            param = PARAMS_BY_KEY.get(key)
            if param is None:
                raise ParamError(f"unknown parameter '{key}'")
            setattr(out, key, param.coerce(raw))
        return out

    @classmethod
    def defaults(cls) -> BrewParams:
        return cls()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def rows(self) -> list[dict[str, Any]]:
        """The parameter table as a front-end wants to render it: one row per Param,
        carrying the label, the formatted value, the hint and the input bounds."""
        return [{
            "key": p.key,
            "label": p.label,
            "value": getattr(self, p.key),
            "display": p.show(getattr(self, p.key)),
            "hint": p.hint,
            "unit": p.unit.strip(),
            "choices": list(p.choices) if p.choices else None,
            "default": p.default,
            "default_display": p.show(p.default),
            "is_default": getattr(self, p.key) == p.default,
            "lo": p.lo,
            "hi": p.hi,
            "step": p.step,
        } for p in PARAMS]


def derived_name(p: BrewParams) -> str:
    return p.name or f"Gen {p.dose:g}g 1:{p.ratio:g} {p.pattern} {p.pours}p"


def build_pours(p: BrewParams) -> list[dict[str, Any]]:
    """Bloom + main pours. The 'dominant' pattern covers the bloom and every main pour
    except the FIRST one, which gets the other pattern."""
    other = "center" if p.pattern == "spiral" else "spiral"
    patterns = [p.pattern, other] + [p.pattern] * (p.pours - 1)
    bloom_ml, main_ml, _ = volumes(p.dose, p.ratio, p.pours)
    return [{
        "label": "Bloom" if i == 0 else f"Pour {i}",
        "ml": bloom_ml if i == 0 else main_ml,
        "temp_c": int(p.temp) - i,                      # one degree cooler each pour
        "pattern": pattern,
        "pause_s": int(p.bloom_time) if i == 0 else int(p.pause),
        # rpm is the holder's rotation speed, meaningful only for a pattern that
        # traces a circle; a center pour must send 0.
        "rpm": 0 if pattern == "center" else int(p.rpm),
        "flow_ml_s": float(p.flow),
        # "No agitation or vibrations": both byte-3 bits stay clear.
        "agitation": False,
        "vibrate_before": False,
    } for i, pattern in enumerate(patterns)]


def to_yaml(p: BrewParams) -> str:
    """The recipe as YAML text, header comments included — the file that gets cached."""
    pours = build_pours(p)
    bloom_ml, main_ml, total = volumes(p.dose, p.ratio, p.pours)
    mult = bloom_multiplier(p.dose)
    name = derived_name(p)
    # A web front-end will happily send dose 0 while someone is mid-typing, and the
    # product of that keystroke must be the validator's message, not a traceback.
    bloom_x = (bloom_ml / p.dose) if p.dose else 0.0
    lines = [
        f"# {name}",
        "#",
        f"# Generated by xbloom brewgen — {p.pattern}-dominant, {p.pours} main pours.",
        f"# Bloom {bloom_ml} ml = {bloom_x:.2f}x dose (formula target "
        f"{mult:.2f}x = {round(p.dose * mult)} ml; the bloom carries the rounding so the",
        f"# {p.pours} main pours come out exactly equal at {main_ml} ml and Σ = {total} ml "
        f"= dose x ratio).",
        f"# Temps step down 1 C per pour from {int(p.temp)} C. No vibration on any pour.",
        # The parameters, verbatim, so the file can be read back into the generator.
        # A comment, so it is not part of the recipe the machine sees and not part of
        # fingerprint(): two files that brew the same thing still match.
        params_line(p),
        f"name: {name}",
        f"dose_g: {p.dose:g}",
        f"grind: {p.grind}",
        f"ratio: {p.ratio:g}",
        "pours:",
    ]
    for pour in pours:
        lines.append("  - {" + ", ".join([
            f"label: {pour['label']}", f"ml: {pour['ml']}", f"temp_c: {pour['temp_c']}",
            f"pattern: {pour['pattern']}", f"pause_s: {pour['pause_s']}", f"rpm: {pour['rpm']}",
            f"flow_ml_s: {pour['flow_ml_s']}", "agitation: false", "vibrate_before: false",
        ]) + "}")
    return "\n".join(lines) + "\n"


PARAMS_MARK = "# params: "


def params_line(p: BrewParams) -> str:
    """The parameter set as one comment line, e.g. ``# params: dose=10 ratio=17 ...``.

    This is what makes a cached file *editable* rather than merely brewable. The YAML
    body is pours — the generator's output — and several parameters (bloom_time, pause,
    rpm) only reach it indirectly, so recovering them from the pours means reversing the
    formulas. Writing them down instead costs one line and cannot drift.
    """
    def fmt(v: Any) -> str:
        return f"{v:g}" if isinstance(v, float) else str(v)
    return PARAMS_MARK + " ".join(f"{k}={fmt(getattr(p, k))}" for k in param_keys())


def params_from_yaml(text: str) -> BrewParams | None:
    """The parameters a cached recipe was generated from, or None if they can't be
    recovered — a hand-written recipe, or one this generator did not produce.

    Files written since :func:`params_line` existed carry them outright. For an older
    file there is a fallback, but it is only trusted when it *proves* itself: the
    recovered parameters are re-rendered and the result must match the file's own pours
    block. A recipe we would regenerate differently is not one we offer to edit in
    place, because editing it would silently rewrite what it brews.
    """
    for line in text.splitlines():
        if line.startswith(PARAMS_MARK):
            data: dict[str, Any] = {}
            for pair in line[len(PARAMS_MARK):].split():
                key, _, val = pair.partition("=")
                if key in PARAMS_BY_KEY:
                    data[key] = val
            try:
                out = BrewParams.from_mapping(data)
            except ParamError:
                return None
            return _with_name(out, text)
    return _recovered(text)


def _recovered(text: str) -> BrewParams | None:
    """Best-effort reconstruction for a file with no ``# params:`` line, self-checked
    against the file's pours. → None unless the round-trip is exact."""
    body = _pours_block(text)
    if len(body) < 2:
        return None
    try:
        head = dict(re.findall(r"^(dose_g|ratio|grind): (\S+)$", text, re.M))
        bloom, first, last = body[0], body[1], body[-1]
        out = BrewParams(
            dose=float(head["dose_g"]), ratio=float(head["ratio"]),
            grind=int(head["grind"]), temp=int(bloom["temp_c"]),
            bloom_time=int(bloom["pause_s"]), pattern=bloom["pattern"],
            pours=len(body) - 1, pause=int(first["pause_s"]),
            rpm=int(last["rpm"]) or int(first["rpm"]), flow=float(first["flow_ml_s"]),
        )
    except (KeyError, ValueError, IndexError):
        return None
    return _with_name(out, text) if _pours_block(to_yaml(out)) == body else None


def _with_name(p: BrewParams, text: str) -> BrewParams:
    """Attach the file's ``name:`` — but only when it is *custom*. A name equal to the
    derived one must stay None, or cache_filename() would append its slug and a fork of
    an unrenamed brew would file itself under a doubled name."""
    nm = cached_name_from(text)
    p.name = nm if nm and nm != derived_name(p) else None
    return p


def _pours_block(text: str) -> list[dict[str, str]]:
    """The ``pours:`` entries as plain key→string dicts. Parsing the one inline-mapping
    shape :func:`to_yaml` writes; anything else yields nothing, which is the answer."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^  - \{(.+)\}$", line)
        if m:
            out.append(dict(kv.split(": ", 1) for kv in m.group(1).split(", ") if ": " in kv))
    return out


def cached_name_from(text: str) -> str | None:
    m = re.search(r"^name: (.+)$", text, re.M)
    return m.group(1).strip() if m else None


def validate_yaml(text: str) -> str | None:
    """→ None if the recipe is valid, else the validator's message. Uses the real
    Recipe model, so a front-end shows exactly what ``xbloom validate`` would say."""
    import yaml

    from .recipe import Recipe, RecipeError
    try:
        Recipe.from_dict(yaml.safe_load(text)).validate()
    except RecipeError as exc:
        return str(exc)
    return None


def fingerprint(text: str) -> str:
    """A stable identity for *what a recipe brews*, derived from its YAML text.

    Comments and the ``name:`` line are stripped: they describe the recipe, they are not
    part of it. Two files with the same fingerprint put the same water, at the same
    temperature, through the same grounds — whatever they happen to be called. That is
    the question a front-end is asking when it wants to know "have I brewed this before?",
    and it is a stricter test than comparing cache filenames, which carry today's date.
    """
    keep = [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and not line.startswith("name:")
    ]
    return "\n".join(keep)


def oversize(p: BrewParams) -> int | None:
    """The largest pour, if any pour exceeds the 127 ml the app will send."""
    bloom_ml, main_ml, _ = volumes(p.dose, p.ratio, p.pours)
    biggest = max(bloom_ml, main_ml)
    return biggest if biggest > APP_MAX_POUR_ML else None


# --- the plan: one object both front-ends render -----------------------------
@dataclass
class BrewPlan:
    """Everything derived from one :class:`BrewParams` — computed once, rendered many
    ways. A front-end needs no arithmetic of its own, so the terminal and the web UI
    cannot disagree about what a given set of parameters brews."""

    params: BrewParams
    name: str
    pours: list[dict[str, Any]]
    bloom_ml: int
    main_ml: int
    total_ml: int
    bloom_x_dose: float
    bloom_target_x: float
    yaml: str
    error: str | None            # None when the recipe is valid
    oversize_ml: int | None      # set when some pour exceeds APP_MAX_POUR_ML
    filename: str                # the cache filename this plan would be written to

    @property
    def valid(self) -> bool:
        return self.error is None

    def to_json(self) -> dict[str, Any]:
        return {
            "params": self.params.as_dict(),
            "rows": self.params.rows(),
            "name": self.name,
            "pours": self.pours,
            "bloom_ml": self.bloom_ml,
            "main_ml": self.main_ml,
            "total_ml": self.total_ml,
            "n_main": self.params.pours,
            "bloom_x_dose": round(self.bloom_x_dose, 2),
            "bloom_target_x": round(self.bloom_target_x, 2),
            "dose_g": self.params.dose,
            "ratio": self.params.ratio,
            "grind": self.params.grind,
            "yaml": self.yaml,
            "error": self.error,
            "valid": self.valid,
            "oversize_ml": self.oversize_ml,
            "max_pour_ml": APP_MAX_POUR_ML,
            "filename": self.filename,
            "fingerprint": fingerprint(self.yaml),
        }


def plan(p: BrewParams) -> BrewPlan:
    """Compute the full plan for these parameters, validation included."""
    bloom_ml, main_ml, total = volumes(p.dose, p.ratio, p.pours)
    text = to_yaml(p)
    return BrewPlan(
        params=p,
        name=derived_name(p),
        pours=build_pours(p),
        bloom_ml=bloom_ml,
        main_ml=main_ml,
        total_ml=total,
        # Guard the division: dose 0 is invalid anyway, and Recipe.validate() is what
        # reports it — building the plan must not blow up before that message is read.
        bloom_x_dose=(bloom_ml / p.dose) if p.dose else 0.0,
        bloom_target_x=bloom_multiplier(p.dose),
        yaml=text,
        error=validate_yaml(text),
        oversize_ml=oversize(p),
        filename=cache_filename(p),
    )


# --- the cache --------------------------------------------------------------
def name_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:24] or "named"


def cache_filename(p: BrewParams) -> str:
    """``<date>[-<non-default params>].yaml`` — e.g. ``2026-09-04-dose15-ratio16.yaml``.

    Only parameters that differ from their default appear, so an all-defaults brew is
    just the date and a tweaked one reads at a glance. Because the name is derived from
    the *complete* parameter state (defaults included, by omission), two runs sharing a
    filename necessarily share content — which is what makes writing it a cache rather
    than an overwrite.
    """
    parts = []
    for param in PARAMS:
        value = getattr(p, param.key)
        if value == param.default:
            continue
        if param.key == "pattern":
            parts.append(str(value))            # "center" needs no label to be clear
        else:
            key = param.key.replace("_", "")
            parts.append(f"{key}{value:g}" if isinstance(value, float) else f"{key}{value}")
    if p.name:
        # A custom name changes the file's contents, so it has to change the filename
        # too or the same-name-same-content invariant breaks.
        parts.append(name_slug(p.name))
    return "-".join([date.today().isoformat(), *parts]) + ".yaml"


def cache_dir(root: str | Path | None = None) -> Path:
    """Where run recipes are cached. Defaults to the package's state dir — the same
    place brew history and the dial presets live ("persists but isn't precious"),
    which keeps generated files out of the curated ``recipes/`` tree."""
    if root:
        return Path(root).expanduser()
    from . import paths
    return paths.state_dir() / "brews"


def cache_path(p: BrewParams, root: str | Path | None = None) -> Path:
    return cache_dir(root) / cache_filename(p)


def write_cached(p: BrewParams, root: str | Path | None = None,
                 out: str | Path | None = None) -> tuple[Path, bool]:
    """Write the recipe to the cache (or to ``out``). → (path, unchanged).

    ``unchanged`` is True when the file was already byte-identical, which is the common
    case for a repeated brew and worth saying out loud rather than reporting a write.
    """
    text = to_yaml(p)
    path = Path(out).expanduser() if out else cache_path(p, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return path, True
    path.write_text(text, encoding="utf-8")
    return path, False


BREWED_LEDGER = ".brewed.json"


def _ledger(root: str | Path | None) -> dict[str, float]:
    """``{filename: epoch seconds}`` of when each cached recipe last *started a brew*.

    It lives beside the recipes rather than inside them on purpose. A recipe file is the
    record of what a brew was (see :func:`renamed_copy`) and is also rewritten by
    autosave, so neither its contents nor its mtime can answer "when did I last brew
    this?" — editing a brew would otherwise count as brewing it. The leading dot keeps
    it out of the ``*.yaml`` glob and past :func:`resolve_cached`'s dotfile refusal.
    """
    try:
        data = json.loads((cache_dir(root) / BREWED_LEDGER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_ledger(root: str | Path | None, ledger: dict[str, float]) -> None:
    (cache_dir(root) / BREWED_LEDGER).write_text(json.dumps(ledger, indent=1), encoding="utf-8")


def mark_brewed(path: Path, root: str | Path | None = None, when: float | None = None) -> None:
    """Record that ``path`` just started a brew. Called only from the start route."""
    ledger = _ledger(root)
    ledger[path.name] = when if when is not None else datetime.now().timestamp()
    _save_ledger(root, ledger)


def _forget_or_move(root: str | Path | None, old: str, new: str | None) -> None:
    """Keep the ledger true across a rename (``new`` set) or a delete (``new`` None)."""
    ledger = _ledger(root)
    if old not in ledger:
        return
    when = ledger.pop(old)
    if new:
        ledger[new] = when
    _save_ledger(root, ledger)


def cached_name(path: Path) -> str | None:
    """The ``name:`` a cached recipe already carries, for use as a gate's default."""
    m = re.search(r"^name: (.+)$", path.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else None


def renamed_copy(path: Path, new_name: str, root: str | Path | None = None) -> Path:
    """``path`` unchanged if the name is already right, else a fresh cache entry
    carrying ``new_name``.

    Renaming writes a NEW file rather than editing the original: the cached recipe is
    the record of a brew that already happened, so it stays byte-stable.
    """
    text = path.read_text(encoding="utf-8")
    if cached_name(path) == new_name:
        return path
    text = re.sub(r"^name: .+$", f"name: {new_name}", text, count=1, flags=re.M)
    out = cache_dir(root) / f"{date.today().isoformat()}-{name_slug(new_name)}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def rename_cached(path: Path, new_name: str, root: str | Path | None = None) -> Path:
    """Rename a cached brew **in place**: rewrite its ``name:`` and move the file to
    match. → the new path (``path`` itself when the name was already right).

    This is the deliberate counterpart to :func:`renamed_copy`. That one is used on the
    brew path, where the cached file is the record of something that already happened and
    must stay byte-stable, so it forks. This one is the user editing a label in a list:
    they mean *this entry*, and leaving a stale duplicate behind would be the surprise.

    The fingerprint (:func:`fingerprint`) is untouched by a rename, so a renamed entry
    still matches the same parameters.
    """
    new_name = new_name.strip()
    if not new_name:
        raise ParamError("a brew needs a name")
    if "\n" in new_name or len(new_name) > 80:
        raise ParamError("that name is too long, or has a line break in it")
    text = path.read_text(encoding="utf-8")
    if cached_name(path) == new_name:
        return path
    text = re.sub(r"^name: .+$", f"name: {new_name}", text, count=1, flags=re.M)
    # The header comment is the recipe's title too; keep it from contradicting `name:`.
    text = re.sub(r"\A# .+$", f"# {new_name}", text, count=1, flags=re.M)

    stamp = path.name.split("-")[:3]
    prefix = "-".join(stamp) if len(stamp) == 3 else date.today().isoformat()
    out = path.with_name(f"{prefix}-{name_slug(new_name)}.yaml")
    n = 2
    while out.exists() and out != path:        # two brews may share a slug on one day
        out = path.with_name(f"{prefix}-{name_slug(new_name)}-{n}.yaml")
        n += 1
    path.write_text(text, encoding="utf-8")
    if out != path:
        path.rename(out)
        _forget_or_move(root, path.name, out.name)
    return out



def save_cached(path: Path, p: BrewParams) -> Path:
    """Write ``p`` over an existing cache entry, keeping its custom name. → ``path``.

    This is the autosave behind an edited staged brew. The **filename does not move**:
    it records which parameters were non-default when the entry was created, and
    rewriting it on every keystroke would leave a trail of files and break any link
    anyone holds. :func:`rename_cached` is the one thing that moves a file.
    """
    if not path.exists():
        raise ParamError(f"{path.name} is gone — nothing to save over")
    text = path.read_text(encoding="utf-8")
    p = replace(p)
    p.name = cached_name_from(text)
    # A name that was just the old parameters' derived name was never chosen — it keeps
    # tracking the parameters. Anything else is a name somebody typed, and stays.
    before = params_from_yaml(text)
    if before is not None and p.name == derived_name(replace(before, name=None)):
        p.name = None
    path.write_text(to_yaml(p), encoding="utf-8")
    return path


def fork_cached(p: BrewParams, root: str | Path | None = None,
                name: str | None = None) -> Path:
    """Copy the current parameters into a **new** cache entry. → its path.

    The point of a fork is to stop editing what you had and start editing a copy, so
    unlike :func:`write_cached` it never hands back an existing file. The copy's *name*
    gets the "copy" suffix; its *filename* gets a plain ``-copy``/``-copy-2``, rather
    than the name's slug, so a chain of forks does not grow a filename to match.
    """
    base = (name or derived_name(p)).strip()
    if not base:
        raise ParamError("a fork needs a name")
    plain = derived_name(replace(p, name=None))
    stem = cache_path(replace(p, name=None if base == plain else base), root).stem
    out = replace(p, name=f"{base} copy")
    path = cache_dir(root) / f"{stem}-copy.yaml"
    n = 2
    while path.exists():
        out = replace(p, name=f"{base} copy {n}")
        path = cache_dir(root) / f"{stem}-copy-{n}.yaml"
        n += 1
    return write_cached(out, root, out=path)[0]


def delete_cached(path: Path, root: str | Path | None = None) -> None:
    """Remove one cache entry. Only ever reached through :func:`resolve_cached`, so the
    path is a bare filename inside the cache directory and nothing else."""
    if path.suffix != ".yaml":
        raise ParamError("that is not a cached recipe")
    path.unlink(missing_ok=True)
    _forget_or_move(root, path.name, None)


@dataclass
class CachedBrew:
    """One cached recipe file, summarised for a list."""

    path: Path
    summary: str                 # "10 g  1:17  170 ml  4 pours  pre-ground", or why not
    age: str                     # "today" / "yesterday" / "5d ago" — since brewed, else saved
    readable: bool
    name: str | None = None
    fingerprint: str = ""        # see fingerprint(): identity of what it brews
    brewed: bool = False         # has it ever started a brew? (age then counts from that)

    def to_json(self) -> dict[str, Any]:
        return {"file": self.path.name, "stem": self.path.stem, "summary": self.summary,
                "age": self.age, "readable": self.readable, "name": self.name,
                "fingerprint": self.fingerprint, "brewed": self.brewed}


def describe(path: Path) -> tuple[str, bool]:
    """One-line summary of a cached recipe, or why it can't be read. → (text, ok)."""
    from .recipe import Recipe, RecipeError
    try:
        r = Recipe.from_source(str(path))
        grind = "pre-ground" if r.grind == 0 else f"grind {r.grind}"
        return (f"{r.dose_g:g} g  1:{r.effective_ratio:g}  {r.total_water_ml} ml  "
                f"{len(r.pours)} pours  {grind}"), True
    except (RecipeError, OSError, ValueError) as exc:
        return f"unreadable — {exc}", False


def age(path: Path, when: float | None = None) -> str:
    """Days since ``when`` (an epoch), or since the file was last written."""
    stamp = when if when is not None else path.stat().st_mtime
    days = (datetime.now() - datetime.fromtimestamp(stamp)).days
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days}d ago"


def recent_brews(root: str | Path | None = None, limit: int = 5) -> list[CachedBrew]:
    """Cached recipes, most recently **brewed** first.

    Anything that has never started a brew follows, newest-written first — it is still
    worth listing, it just has not earned a place above something you actually made.
    """
    d = cache_dir(root)
    if not d.is_dir():
        return []
    ledger = _ledger(root)
    # (has been brewed, when): True sorts above False, then the later time first.
    files = sorted(
        d.glob("*.yaml"),
        key=lambda f: (f.name in ledger, ledger.get(f.name, f.stat().st_mtime)),
        reverse=True)[:limit]
    out = []
    for f in files:
        summary, ok = describe(f)
        text = f.read_text(encoding="utf-8") if ok else ""
        out.append(CachedBrew(path=f, summary=summary, age=age(f, ledger.get(f.name)),
                              readable=ok, name=cached_name(f) if ok else None,
                              fingerprint=fingerprint(text) if ok else "",
                              brewed=f.name in ledger))
    return out


def resolve_cached(filename: str, root: str | Path | None = None) -> Path:
    """Resolve one cache entry by *bare filename*. → ParamError if it isn't one.

    A front-end may pass this straight from an HTTP request, so the name is checked
    against the directory listing rather than merely being joined onto it: no path
    separators, no traversal, and nothing outside the cache can be reached.
    """
    if filename != Path(filename).name or filename.startswith("."):
        raise ParamError(f"not a cache filename: {filename!r}")
    path = cache_dir(root) / filename
    if not path.is_file():
        raise ParamError(f"no such cached brew: {filename}")
    return path


# --- handing one recipe file to the machine ---------------------------------
def brew_argv(path: str | Path, *, start: bool = False, debug: bool = True,
              address: str | None = None, timeout: float = 600.0) -> list[str]:
    """The ``xbloom brew`` command for one recipe file.

    Load-only unless ``start=True``: this is the single place either front-end can
    append ``--start``, and it appends it nowhere else.
    """
    argv = [sys.executable, "-m", "xbloom_ble.cli", "-v", "brew", str(path),
            "--timeout", str(timeout)]
    if start:
        argv.append("--start")
    if debug:
        argv.append("--debug")
    if address:
        argv += ["--address", address]
    return argv


class StartRefused(RuntimeError):
    """A remote start was asked for with nobody established as present."""


UNATTENDED_REFUSAL = (
    "REFUSING to start a brew: nothing established that a human is present to answer "
    "the confirmation, so a scripted or accidental invocation could dispense boiling "
    "water with nobody in the room."
)


def run_brew(path: str | Path, *, start: bool = False, attended: bool = False,
             debug: bool = True, address: str | None = None, timeout: float = 600.0,
             dry_run: bool = False,
             runner: Callable[[list[str]], int] | None = None) -> tuple[list[str], int]:
    """Hand one recipe file to ``xbloom brew``. → (argv, exit code).

    The single place in the generator that touches hardware — which is also why the
    start guard lives here rather than further up: a guard further up is one a new call
    path can be added around.

    ``attended`` is the caller's assertion that a person asked for this brew and can
    see the machine. It is a parameter and not a check because only the front-end can
    know: the terminal guide requires a real TTY (or an explicit ``--unattended``), the
    web UI requires the brew's name typed back into a confirmation box. A ``start``
    without it raises :class:`StartRefused` before any command runs.
    """
    if start and not attended:
        raise StartRefused(UNATTENDED_REFUSAL)
    argv = brew_argv(path, start=start, debug=debug, address=address, timeout=timeout)
    if dry_run:
        return argv, 0
    run = runner or subprocess.call
    return argv, run(argv)


def param_keys() -> list[str]:
    return [p.key for p in PARAMS]


def brewparams_fields() -> list[str]:
    return [f.name for f in fields(BrewParams)]

#!/usr/bin/env python3
"""Terminal front-end for the recipe generator — every parameter on one screen.

Run it with no arguments for the interactive guide, with the volumes and validity
recomputed live::

    scripts/gen-brew.py                 # the guide
    scripts/gen-brew.py --dose 15       # one-shot: set params on the command line
    scripts/gen-brew.py --dry-run       # print the YAML and stop

All the arithmetic, the YAML, the cache and the ``xbloom brew`` command itself live in
:mod:`xbloom_ble.brewgen`; this file is only the screen and the keystrokes. The same API
backs ``xbloom web`` (a local web UI), so the two cannot drift.

Safety: this only ever *loads* the recipe — you approve on the machine. ``--start`` (or
[s] in the guide, behind a confirmation) commits and starts remotely, which dispenses
near-boiling water. A remote start needs a real terminal, or an explicit
``--unattended``, because a piped stdin answers a prompt just as happily as a person.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from xbloom_ble import brewgen
except ImportError:                                   # pragma: no cover - setup hint
    print("ERROR: xbloom_ble not importable — run `pip install -e .` first", file=sys.stderr)
    raise SystemExit(2) from None

from xbloom_ble.brewgen import PARAMS, BrewParams, StartRefused

PICK_LETTERS = "abcde"


def shell_path(p: Path) -> str:
    """Home-relative and backslash-escaped, so it pastes straight into cd/ls.

    The cache lives under "Application Support" on macOS, and an unescaped space
    there makes the printed path useless for navigating to — you have to re-quote it
    by hand every time."""
    try:
        p = Path("~") / p.relative_to(Path.home())
    except ValueError:
        pass
    return str(p).replace(" ", r"\ ")


# --- the interactive guide ---------------------------------------------------
def render(params: BrewParams, root: str | None) -> list[brewgen.CachedBrew]:
    plan = brewgen.plan(params)

    print("\n  xBloom recipe generator\n")
    print(f"  {'#':>2}  {'parameter':<11} {'value':<10} notes")
    print(f"  {'─' * 2}  {'─' * 11} {'─' * 10} {'─' * 42}")
    for i, p in enumerate(PARAMS, 1):
        print(f"  {i:>2}  {p.label:<11} {p.show(getattr(params, p.key)):<10} {p.hint}")

    print(f"\n  {plan.name}")
    for pour in plan.pours:
        print(f"      {pour['label']:<7} {pour['ml']:>3} ml  {pour['temp_c']} C  "
              f"{pour['pattern']:<6} pause {pour['pause_s']:>2}s  rpm {pour['rpm']:>3}")
    print(f"\n  bloom {plan.bloom_ml} ml + {params.pours} x {plan.main_ml} ml = "
          f"{plan.total_ml} ml   (bloom {plan.bloom_x_dose:.2f}x dose, "
          f"target {plan.bloom_target_x:.2f}x)")
    print(f"  → {shell_path(brewgen.cache_path(params, root))}")

    if plan.error:
        print(f"\n  ✗ invalid: {plan.error}")
    if plan.oversize_ml:
        print(f"\n  ⚠ {plan.oversize_ml} ml pour — over the {brewgen.APP_MAX_POUR_ML} ml the "
              f"app sends. The protocol\n    splits it, but that frame shape is untested "
              f"on hardware.")

    brews = brewgen.recent_brews(root)
    if brews:
        print(f"\n  recent brews — {shell_path(brewgen.cache_dir(root))}")
        for letter, b in zip(PICK_LETTERS, brews, strict=False):
            desc = b.summary if len(b.summary) <= 44 else b.summary[:41] + "..."
            print(f"   {letter}  {b.path.name:<34} {desc:<44} {b.age}")

    quick = f"  [a-{PICK_LETTERS[len(brews) - 1]}] re-brew a recent one\n" if brews else ""
    print(f"\n{quick}  [1-{len(PARAMS)}] edit   [y] show yaml   [g] load onto machine   "
          f"[s] load + START brew\n  [r] reset     [q] quit")
    return brews


def edit(params: BrewParams, p: brewgen.Param) -> None:
    current = getattr(params, p.key)
    prompt = f"  {p.label}"
    if p.choices:
        prompt += f" {'/'.join(str(c) for c in p.choices)}"
    try:
        raw = input(f"{prompt} [{p.show(current)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not raw:
        return
    try:
        setattr(params, p.key, p.coerce(raw))
    except brewgen.ParamError as exc:
        print(f"  ✗ {exc}")


# Sentinel: the gate was cancelled, as distinct from "" meaning "keep the default".
CANCELLED = object()


def start_gate(default_name: str):
    """The gate every remote start passes through: offer to name the brew.

    → the chosen name (the default when the answer is empty), or :data:`CANCELLED`.

    Naming doubles as the confirmation. Remote start commits and fires ``0x46``, so it
    stays a deliberate second action distinct from a plain load — and the pause is
    spent on something useful, since the name lands in both the recipe and the cache
    filename and is what you will be reading back in the brew log later.

    Ctrl-C / EOF cancels: ``input()`` raises there rather than returning "", so an
    interrupted or non-interactive stdin can never be mistaken for consent.
    """
    print("\n  ⚠️  This COMMITS and STARTS the brew remotely — the machine will"
          "\n     dispense near-boiling water. Water, cup and coffee in place?")
    try:
        answer = input(f"  name this brew [{default_name}]  (ctrl-c to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled.")
        return CANCELLED
    return answer or default_name


def rebrew(a, params: BrewParams, brew: brewgen.CachedBrew):
    """Offer to re-run an already-cached recipe. → exit code once it brews, else None.

    The file is brewed AS IT IS ON DISK — no regeneration — so what ran last time is
    exactly what runs now, even if this script's defaults or formulas have since
    changed. That is the point of keeping the cache.
    """
    path = brew.path
    print(f"\n  {path.name}\n  {brew.summary}")
    if not brew.readable:
        input("  (enter) ")
        return None
    try:
        pick = input("  enter to load, [s] to load + START, anything else to go back: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    pick = pick.strip().lower()
    if pick not in ("", "s"):
        return None
    if pick == "s":
        chosen = start_gate(brew.name or path.stem)
        if chosen is CANCELLED:
            return None
        path = brewgen.renamed_copy(path, chosen, a.cache_dir)
        if path.name != brew.path.name:
            print(f"  saved as {shell_path(path)}")
    # Deliberately NOT via the generate path: that would rebuild the recipe from the
    # parameters currently on screen and write the result over this file, so picking a
    # cached brew would silently brew something else and destroy the cache entry.
    return hand_off(a, path, start=pick == "s")


def guide(a, params: BrewParams) -> int:
    """Loop until the user loads, starts, or quits."""
    while True:
        brews = render(params, a.cache_dir)
        try:
            choice = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  bye")
            return 0

        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "r":
            params = BrewParams.defaults()
            continue
        if choice == "y":
            print()
            print(brewgen.to_yaml(params), end="")
            input("  (enter) ")
            continue
        if choice in ("g", "s"):
            plan = brewgen.plan(params)
            if not plan.valid:
                print(f"\n  ✗ cannot brew an invalid recipe: {plan.error}")
                input("  (enter) ")
                continue
            if choice == "s":
                chosen = start_gate(plan.name)
                if chosen is CANCELLED:
                    continue
                # Setting the name feeds both the recipe's `name:` and the cache
                # filename, so the brew is filed under what you just called it. Only
                # set it when it actually differs from the derived name: assigning the
                # default back would make cache_filename treat it as a custom name and
                # append its slug, so "enter to keep the default" would rename the file.
                if chosen != plan.name:
                    params.name = chosen
            return generate_and_run(a, params, start=choice == "s")
        if choice.isdigit() and 1 <= int(choice) <= len(PARAMS):
            edit(params, PARAMS[int(choice) - 1])
            continue
        if len(choice) == 1 and choice in PICK_LETTERS[:len(brews)]:
            rc = rebrew(a, params, brews[PICK_LETTERS.index(choice)])
            if rc is not None:
                return rc
            continue
        print(f"  ? '{choice}'")


# --- generate + hand off to the CLI -----------------------------------------
def generate_and_run(a, params: BrewParams, *, start: bool) -> int:
    plan = brewgen.plan(params)
    if plan.bloom_ml < 1:
        print(f"ERROR: bloom works out at {plan.bloom_ml} ml — raise --ratio or lower "
              f"--pours", file=sys.stderr)
        return 2
    if not plan.valid:
        print(f"Generated recipe is INVALID:\n{plan.yaml}\n{plan.error}", file=sys.stderr)
        return 1

    print(plan.yaml, end="")
    print(f"# bloom {plan.bloom_ml} ml + {params.pours} x {plan.main_ml} ml = "
          f"{plan.total_ml} ml ({params.dose:g} g x {params.ratio:g})")
    if plan.oversize_ml:
        print(f"\n⚠️  a pour exceeds {brewgen.APP_MAX_POUR_ML} ml ({plan.oversize_ml} ml). The "
              f"protocol splits it, but the app never sends pours that large — untested "
              f"on hardware.", file=sys.stderr)
    if a.dry_run:
        print(f"\n# would write: {brewgen.cache_path(params, a.cache_dir)}")
        return 0

    path, unchanged = brewgen.write_cached(params, a.cache_dir, a.out)
    print(f"\n{'Cached (unchanged)' if unchanged else 'Cached'}: {path}")
    return hand_off(a, path, start=start)


def hand_off(a, path: Path, *, start: bool) -> int:
    """Run one recipe file through ``brewgen.run_brew`` — the single hardware entry point.

    Attendance (what lets a remote start through) is decided here, because only the
    front-end knows: a real terminal means the gate's prompt was answered by a person;
    ``--unattended`` is the explicit, greppable way to say "I mean it" from automation.
    """
    attended = sys.stdin.isatty() or a.unattended
    try:
        argv, rc = brewgen.run_brew(
            path, start=start, attended=attended, debug=a.debug, address=a.address,
            timeout=a.timeout, dry_run=a.no_exec or a.dry_run,
        )
    except StartRefused as exc:
        print(f"{exc}\nPass --unattended if you really mean to start unsupervised.",
              file=sys.stderr)
        return 2
    print(f"$ {' '.join(argv)}\n")
    # --no-exec covers every path into the machine, including re-brewing a cached file,
    # which reaches this function without passing the --dry-run check in
    # generate_and_run. It is what makes the interactive flows safe to exercise.
    if a.no_exec or a.dry_run:
        print("(--no-exec: not run)" if a.no_exec else "(--dry-run: not run)")
    return rc


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(
        description="Generate a recipe from a few parameters and load it onto the machine. "
                    "With no arguments, opens an interactive guide. "
                    "`xbloom web` is the same thing in a browser.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for p in PARAMS:                       # same table as the guide and the web UI
        ap.add_argument(p.flag, type=p.type, default=p.default, choices=p.choices,
                        help=p.hint)
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="open the guide even when other flags are given")
    ap.add_argument("--name", help="recipe name (default: derived from the parameters)")
    ap.add_argument("-o", "--out", help="write the YAML here instead of the cache")
    ap.add_argument("--cache-dir", help="directory for cached run recipes "
                                        "(default: the xbloom state dir)")
    ap.add_argument("--dry-run", action="store_true", help="print the recipe and stop")
    ap.add_argument("--no-exec", action="store_true",
                    help="do everything except run the brew — print the command instead. "
                         "Use this to exercise the interactive flows safely")
    ap.add_argument("--unattended", action="store_true",
                    help="allow --start with no terminal attached (automation only; "
                         "nobody will be prompted before hot water is dispensed)")
    ap.add_argument("--start", action="store_true",
                    help="also START the brew remotely — dispenses hot water")
    # On by default: when a brew misbehaves, the BLE frame log is the only record of
    # what was actually sent, and it cannot be reconstructed after the fact. A log per
    # brew is cheap; re-running a failure blind is not.
    ap.add_argument("--no-debug", dest="debug", action="store_false", default=True,
                    help="don't capture the BLE frame log (xbloom-debug-*.log)")
    ap.add_argument("--address", help="machine BLE address")
    ap.add_argument("--timeout", type=float, default=600.0, help="telemetry stream seconds")
    args = ap.parse_args(argv)

    params = BrewParams.from_mapping(
        {p.key: getattr(args, p.key) for p in PARAMS} | {"name": args.name})

    # No arguments on a terminal means "show me the parameters", not "brew the
    # defaults right now" — the guide is both safer and what you almost always want.
    if args.interactive or (not argv and sys.stdin.isatty()):
        return guide(args, params)

    # One-shot --start passes the same gate, so every remote start in this script is
    # named and confirmed the same way. On a non-terminal there is nobody to prompt, so
    # hand_off() refuses outright rather than starting unsupervised.
    start = args.start
    if start and sys.stdin.isatty():
        chosen = start_gate(brewgen.derived_name(params))
        if chosen is CANCELLED:
            return 1
        if chosen != brewgen.derived_name(params):
            params.name = chosen
    return generate_and_run(args, params, start=start)


if __name__ == "__main__":
    raise SystemExit(main())

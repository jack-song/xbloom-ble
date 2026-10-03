"""Tests for the recipe-generator logic (xbloom_ble.brewgen).

The safety-relevant ones are :func:`test_brew_argv_is_load_only_by_default` and
:func:`test_run_brew_refuses_unattended_start` — they guard the separation CLAUDE.md
requires: nothing in the generator may start a brew as a side effect of loading one.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from xbloom_ble import brewgen
from xbloom_ble.brewgen import BrewParams, ParamError, StartRefused


# --- the parameter table -----------------------------------------------------
def test_params_table_matches_brewparams_fields():
    """ONE table drives argparse, the terminal guide and the web UI. If a Param's key
    stops matching a BrewParams field, from_mapping() silently loses that parameter."""
    assert brewgen.param_keys() == [f.name for f in fields(BrewParams) if f.name != "name"]


def test_params_defaults_match_brewparams_defaults():
    defaults = BrewParams.defaults()
    for p in brewgen.PARAMS:
        assert getattr(defaults, p.key) == p.default, p.key


def test_from_mapping_coerces_strings():
    p = BrewParams.from_mapping({"dose": "15", "pours": "2", "pattern": "center"})
    assert (p.dose, p.pours, p.pattern) == (15.0, 2, "center")
    assert isinstance(p.dose, float) and isinstance(p.pours, int)


def test_from_mapping_rejects_bad_values():
    with pytest.raises(ParamError):
        BrewParams.from_mapping({"pours": 5})            # not in choices
    with pytest.raises(ParamError):
        BrewParams.from_mapping({"dose": "hot"})         # not a float
    with pytest.raises(ParamError):
        BrewParams.from_mapping({"tempurature": 91})     # typo'd key, not ignored
    with pytest.raises(ParamError):
        BrewParams.from_mapping(["dose", 15])            # not an object


def test_from_mapping_is_a_partial_update_over_base():
    base = BrewParams(dose=15.0, pattern="center")
    p = BrewParams.from_mapping({"dose": 12}, base=base)
    assert (p.dose, p.pattern) == (12.0, "center")
    assert base.dose == 15.0                             # the base is not mutated


# --- the arithmetic ----------------------------------------------------------
@pytest.mark.parametrize("dose,ratio,n", [
    (10.0, 17.0, 3), (15.0, 17.0, 3), (18.0, 15.0, 2), (8.0, 16.0, 2), (12.5, 18.0, 3),
])
def test_volumes_add_up_exactly(dose, ratio, n):
    """The machine checks Σ(pour ml) / dose against the ratio byte, and the main pours
    must be exactly equal — so the bloom is the only place rounding may land."""
    bloom, main, total = brewgen.volumes(dose, ratio, n)
    assert bloom + main * n == total
    assert total == round(dose * ratio)


def test_bloom_multiplier_slides_between_the_anchors():
    assert brewgen.bloom_multiplier(8.0) == 3.0          # at/below 10 g
    assert brewgen.bloom_multiplier(18.0) == 2.0         # at/above 15 g
    assert brewgen.bloom_multiplier(12.5) == pytest.approx(2.5)


def test_plan_of_defaults_is_valid_and_specific():
    p = brewgen.plan(BrewParams.defaults())
    assert p.valid and p.error is None
    assert (p.bloom_ml, p.main_ml, p.total_ml) == (29, 47, 170)
    assert [x["ml"] for x in p.pours] == [29, 47, 47, 47]
    assert [x["temp_c"] for x in p.pours] == [91, 90, 89, 88]   # -1 C per pour
    assert p.oversize_ml is None


def test_pour_patterns_and_rpm():
    """The dominant pattern covers the bloom and every main pour but the first; a center
    pour must send rpm 0, since rpm is the holder's rotation speed."""
    p = brewgen.plan(BrewParams(pattern="spiral", pours=3))
    assert [x["pattern"] for x in p.pours] == ["spiral", "center", "spiral", "spiral"]
    assert [x["rpm"] for x in p.pours] == [60, 0, 60, 60]
    c = brewgen.plan(BrewParams(pattern="center", pours=2))
    assert [x["pattern"] for x in c.pours] == ["center", "spiral", "center"]
    assert [x["rpm"] for x in c.pours] == [0, 60, 0]


def test_no_pour_ever_vibrates_or_agitates():
    for pour in brewgen.plan(BrewParams(dose=14.0, ratio=16.0)).pours:
        assert pour["agitation"] is False
        assert pour["vibrate_before"] is False


def test_oversize_flags_a_pour_the_app_would_never_send():
    p = brewgen.plan(BrewParams(dose=18.0, ratio=22.0, pours=2))
    assert p.oversize_ml is not None and p.oversize_ml > brewgen.APP_MAX_POUR_ML


def test_invalid_params_report_instead_of_raising():
    """Building a plan must never blow up — the validator's message is the product."""
    p = brewgen.plan(BrewParams(dose=0.0))
    assert not p.valid and p.error
    assert p.bloom_x_dose == 0.0          # no ZeroDivisionError on the way there


def test_plan_json_is_serialisable():
    import json
    data = brewgen.plan(BrewParams.defaults()).to_json()
    json.loads(json.dumps(data))
    assert data["rows"][0]["key"] == "dose"
    assert data["valid"] is True


# --- the cache --------------------------------------------------------------
def test_cache_filename_lists_only_non_default_params():
    from datetime import date
    today = date.today().isoformat()
    assert brewgen.cache_filename(BrewParams.defaults()) == f"{today}.yaml"
    assert brewgen.cache_filename(BrewParams(dose=15.0, ratio=16.0)) == \
        f"{today}-dose15-ratio16.yaml"
    assert brewgen.cache_filename(BrewParams(pattern="center")) == f"{today}-center.yaml"
    assert "morningbrew" in brewgen.cache_filename(BrewParams(name="Morning Brew!"))


def test_same_filename_implies_same_content(tmp_path):
    """The invariant that makes writing a cache rather than an overwrite."""
    a, b = BrewParams(dose=15.0), BrewParams(dose=15.0)
    assert brewgen.cache_filename(a) == brewgen.cache_filename(b)
    assert brewgen.to_yaml(a) == brewgen.to_yaml(b)


def test_write_cached_reports_unchanged_the_second_time(tmp_path):
    p = BrewParams(dose=15.0)
    path, unchanged = brewgen.write_cached(p, tmp_path)
    assert path.parent == tmp_path and not unchanged
    _, unchanged = brewgen.write_cached(p, tmp_path)
    assert unchanged


def test_recent_brews_reads_back_what_was_written(tmp_path):
    brewgen.write_cached(BrewParams(dose=15.0), tmp_path)
    brews = brewgen.recent_brews(tmp_path)
    assert len(brews) == 1
    assert brews[0].readable and brews[0].age == "today"
    assert "15 g" in brews[0].summary and "255 ml" in brews[0].summary


def test_recent_brews_survives_an_unreadable_file(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: nope\n", encoding="utf-8")
    brews = brewgen.recent_brews(tmp_path)
    assert len(brews) == 1 and not brews[0].readable
    assert "unreadable" in brews[0].summary


def test_renamed_copy_leaves_the_original_byte_stable(tmp_path):
    path, _ = brewgen.write_cached(BrewParams(dose=15.0), tmp_path)
    before = path.read_text(encoding="utf-8")
    out = brewgen.renamed_copy(path, "Sunday Filter", tmp_path)
    assert out != path
    assert path.read_text(encoding="utf-8") == before
    assert brewgen.cached_name(out) == "Sunday Filter"
    # Same name asked for again → the same file, no pointless copy.
    assert brewgen.renamed_copy(out, "Sunday Filter", tmp_path) == out


def test_resolve_cached_refuses_anything_but_a_bare_filename(tmp_path):
    """A web front-end passes this straight from a request, so traversal must not work."""
    path, _ = brewgen.write_cached(BrewParams(dose=15.0), tmp_path)
    assert brewgen.resolve_cached(path.name, tmp_path) == path
    for bad in ["../secrets.yaml", "/etc/passwd", "sub/dir.yaml", ".hidden.yaml"]:
        with pytest.raises(ParamError):
            brewgen.resolve_cached(bad, tmp_path)
    with pytest.raises(ParamError):
        brewgen.resolve_cached("not-there.yaml", tmp_path)


# --- SAFETY: loading is not brewing -----------------------------------------
def test_brew_argv_is_load_only_by_default():
    argv = brewgen.brew_argv("/tmp/r.yaml")
    assert "--start" not in argv
    assert argv[1:7] == ["-m", "xbloom_ble.cli", "-v", "brew", "/tmp/r.yaml",
                         "--timeout"]


def test_brew_argv_adds_start_only_when_asked():
    assert "--start" in brewgen.brew_argv("/tmp/r.yaml", start=True)


def test_run_brew_refuses_unattended_start():
    """Nothing may fire commit+start without a front-end stating a human is present."""
    calls = []
    with pytest.raises(StartRefused):
        brewgen.run_brew("/tmp/r.yaml", start=True, attended=False,
                         runner=lambda argv: calls.append(argv) or 0)
    assert calls == []                      # refused BEFORE anything was executed


def test_run_brew_load_path_needs_no_attendance():
    """A plain load never dispenses water, so it is not gated — and never gains --start."""
    seen = []
    argv, rc = brewgen.run_brew("/tmp/r.yaml", start=False, attended=False,
                                runner=lambda a: seen.append(a) or 0)
    assert rc == 0 and seen == [argv]
    assert "--start" not in argv


def test_run_brew_dry_run_executes_nothing():
    seen = []
    argv, rc = brewgen.run_brew("/tmp/r.yaml", dry_run=True,
                                runner=lambda a: seen.append(a) or 7)
    assert rc == 0 and seen == [] and argv


# --- reading a cached recipe back into parameters ----------------------------
def test_params_round_trip_through_the_yaml():
    p = brewgen.BrewParams(dose=15.0, ratio=16.0, temp=88, bloom_time=60, pattern="center",
                           pours=2, pause=25, rpm=80, flow=4.0, grind=30)
    back = brewgen.params_from_yaml(brewgen.to_yaml(p))
    assert back == p


def test_params_are_recovered_from_a_file_with_no_params_line():
    """Files cached before the parameter line still stage — via the self-checked
    reconstruction, which only answers when re-rendering reproduces the pours."""
    p = brewgen.BrewParams(dose=15.0, ratio=16.0, temp=88)
    older = "\n".join(line for line in brewgen.to_yaml(p).splitlines()
                      if not line.startswith(brewgen.PARAMS_MARK)) + "\n"
    assert brewgen.params_from_yaml(older) == p


def test_params_are_none_for_a_recipe_we_did_not_generate():
    assert brewgen.params_from_yaml("name: Hand\ndose_g: 10\nratio: 17\ngrind: 0\n") is None


def test_the_params_line_is_a_comment_so_it_stays_out_of_the_fingerprint():
    p = brewgen.BrewParams(dose=15.0)
    assert brewgen.PARAMS_MARK not in brewgen.fingerprint(brewgen.to_yaml(p))


def test_save_cached_rewrites_in_place_and_keeps_a_custom_name(tmp_path):
    p = brewgen.BrewParams(dose=15.0, name="Morning")
    path, _ = brewgen.write_cached(p, tmp_path)
    brewgen.save_cached(path, brewgen.BrewParams(dose=15.0, temp=85))
    assert path.exists()                                   # same file, not a new one
    assert brewgen.cached_name(path) == "Morning"
    assert brewgen.params_from_yaml(path.read_text(encoding="utf-8")).temp == 85


def test_fork_cached_never_returns_an_existing_file(tmp_path):
    p = brewgen.BrewParams(dose=15.0)
    first, _ = brewgen.write_cached(p, tmp_path)
    forks = [brewgen.fork_cached(p, tmp_path) for _ in range(3)]
    assert len({first, *forks}) == 4
    assert [brewgen.cached_name(f) for f in forks] == [
        "Gen 15g 1:17 spiral 3p copy", "Gen 15g 1:17 spiral 3p copy 2",
        "Gen 15g 1:17 spiral 3p copy 3"]


def test_delete_cached_removes_the_file(tmp_path):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), tmp_path)
    brewgen.delete_cached(path)
    assert not path.exists()


# --- saved brews are ordered by when they were last *brewed* ------------------
def test_recent_brews_orders_by_last_brewed_not_last_written(tmp_path):
    import os
    a, _ = brewgen.write_cached(brewgen.BrewParams(dose=11.0), tmp_path)
    b, _ = brewgen.write_cached(brewgen.BrewParams(dose=12.0), tmp_path)
    c, _ = brewgen.write_cached(brewgen.BrewParams(dose=13.0), tmp_path)
    for n, f in enumerate((a, b, c)):
        os.utime(f, (1_000_000 + n, 1_000_000 + n))         # written a < b < c
    brewgen.mark_brewed(a, tmp_path, when=2_000_000)        # ...but a was brewed last
    brewgen.mark_brewed(b, tmp_path, when=1_500_000)
    got = brewgen.recent_brews(tmp_path, limit=10)
    assert [x.path for x in got] == [a, b, c]
    assert [x.brewed for x in got] == [True, True, False]


def test_editing_a_brew_does_not_count_as_brewing_it(tmp_path):
    a, _ = brewgen.write_cached(brewgen.BrewParams(dose=11.0), tmp_path)
    b, _ = brewgen.write_cached(brewgen.BrewParams(dose=12.0), tmp_path)
    brewgen.mark_brewed(a, tmp_path, when=1_000)
    brewgen.mark_brewed(b, tmp_path, when=2_000)
    brewgen.save_cached(a, brewgen.BrewParams(dose=11.0, temp=85))   # autosave touches a
    assert [x.path for x in brewgen.recent_brews(tmp_path)] == [b, a]


def test_the_brewed_time_follows_a_rename_and_goes_with_a_delete(tmp_path):
    a, _ = brewgen.write_cached(brewgen.BrewParams(dose=11.0), tmp_path)
    brewgen.mark_brewed(a, tmp_path, when=1_000)
    moved = brewgen.rename_cached(a, "Morning", tmp_path)
    assert brewgen.recent_brews(tmp_path)[0].brewed is True
    brewgen.delete_cached(moved, tmp_path)
    assert brewgen.recent_brews(tmp_path) == []
    assert brewgen._ledger(tmp_path) == {}


def test_the_ledger_is_not_a_recipe(tmp_path):
    a, _ = brewgen.write_cached(brewgen.BrewParams(dose=11.0), tmp_path)
    brewgen.mark_brewed(a, tmp_path)
    assert len(brewgen.recent_brews(tmp_path)) == 1
    with pytest.raises(ParamError):
        brewgen.resolve_cached(brewgen.BREWED_LEDGER, tmp_path)


def test_autosave_keeps_an_unchosen_name_tracking_the_parameters(tmp_path):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=10.0), tmp_path)
    brewgen.save_cached(path, brewgen.BrewParams(dose=12.0))
    assert brewgen.cached_name(path) == "Gen 12g 1:17 spiral 3p"


def test_autosave_does_not_overwrite_a_name_somebody_typed(tmp_path):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=10.0, name="Morning"), tmp_path)
    brewgen.save_cached(path, brewgen.BrewParams(dose=12.0))
    assert brewgen.cached_name(path) == "Morning"

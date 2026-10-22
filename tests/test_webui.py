"""Tests for the local web UI (xbloom_ble.webui), driven over real HTTP.

The safety-relevant ones are :func:`test_load_never_passes_start` (the /api/load route
cannot brew) and the ``test_start_requires_*`` set (the only route that can brew demands
an explicit acknowledgement it could not have invented on its own).

No hardware and no BLE: ``Runner.launch`` is monkeypatched in the fixture to record the
command instead of spawning it, so what these tests assert is exactly the argv that
would have reached ``xbloom brew``.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from xbloom_ble import brewgen, webui


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A live server on an ephemeral port whose launches are recorded, not executed."""
    launched: list[dict] = []

    def fake_launch(self, path, *, start, name, cwd, address, timeout, debug, details=None):
        argv = brewgen.brew_argv(path, start=start, debug=debug, address=address,
                                 timeout=timeout)
        launched.append({"path": Path(path), "start": start, "name": name, "argv": argv})
        run = webui.Run(id=len(launched), argv=argv, recipe=name, file=Path(path).name,
                        started_brew=start, started_at=0.0, details=details or {})
        run.returncode = 0                  # already finished; nothing to poll
        self._run = run
        return run

    monkeypatch.setattr(webui.Runner, "launch", fake_launch)
    httpd = webui._Server(("127.0.0.1", 0), webui._Handler, root=tmp_path, cwd=tmp_path,
                          address="AA:BB:CC:DD:EE:FF", timeout=123.0, debug=False,
                          verbose=False)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01},
                          daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield type("Srv", (), {"base": base, "launched": launched, "root": tmp_path})
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get(server, path):
    try:
        with urllib.request.urlopen(server.base + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post(server, path, body, *, local=True):
    headers = {"Content-Type": "application/json"}
    if local:
        headers[webui.LOCAL_HEADER] = "1"
    req = urllib.request.Request(server.base + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --- reading ----------------------------------------------------------------
def test_page_is_served(server):
    with urllib.request.urlopen(server.base + "/", timeout=10) as r:
        body = r.read().decode()
    assert r.status == 200
    assert "<h1>xBloom brew</h1>" in body
    assert r.headers["Content-Type"].startswith("text/html")


def test_config_reports_the_machine_and_the_defaults(server):
    status, data = get(server, "/api/config")
    assert status == 200
    assert data["address"] == "AA:BB:CC:DD:EE:FF"
    assert data["timeout"] == 123.0
    assert data["defaults"] == brewgen.BrewParams.defaults().as_dict()
    assert [r["key"] for r in data["rows"]] == brewgen.param_keys()


def test_plan_matches_the_library(server):
    status, data = get(server, "/api/plan?dose=15&ratio=16")
    assert status == 200
    expected = brewgen.plan(brewgen.BrewParams(dose=15.0, ratio=16.0)).to_json()
    assert data == expected
    assert data["valid"] is True


def test_plan_reports_an_invalid_recipe_rather_than_failing(server):
    status, data = get(server, "/api/plan?dose=0")
    assert status == 200 and data["valid"] is False and data["error"]


def test_plan_rejects_a_bad_parameter(server):
    status, data = get(server, "/api/plan?pours=9")
    assert status == 400 and "main pours" in data["error"]


def test_recent_lists_the_cache(server):
    brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, data = get(server, "/api/recent")
    assert status == 200 and len(data["brews"]) == 1
    assert data["brews"][0]["readable"] and "15 g" in data["brews"][0]["summary"]


def test_unknown_route_is_404(server):
    status, data = get(server, "/api/nope")
    assert status == 404 and data["error"] == "not found"


# --- SAFETY: /api/load cannot brew ------------------------------------------
def test_load_never_passes_start(server):
    """The load route builds a LOAD-only command. CLAUDE.md's invariant, over HTTP."""
    status, data = post(server, "/api/load", {"params": {"dose": 15}})
    assert status == 200
    assert len(server.launched) == 1
    call = server.launched[0]
    assert call["start"] is False
    assert "--start" not in call["argv"]
    assert data["run"]["started_brew"] is False
    assert data["run"]["mode"] == "load only"


def test_load_writes_the_recipe_it_loaded_to_the_scratch_file(server):
    post(server, "/api/load", {"params": {"dose": 15}})
    path = server.launched[0]["path"]
    assert path.parent == server.root / brewgen.SCRATCH_DIR     # loaded, but not saved
    assert path.read_text(encoding="utf-8") == brewgen.to_yaml(brewgen.BrewParams(dose=15.0))


def test_load_refuses_an_invalid_recipe(server):
    status, data = post(server, "/api/load", {"params": {"dose": 0}})
    assert status == 400 and "invalid recipe" in data["error"]
    assert server.launched == []


def test_load_rejects_a_bad_parameter(server):
    status, data = post(server, "/api/load", {"params": {"pattern": "zigzag"}})
    assert status == 400 and server.launched == []


# --- SAFETY: /api/start is the only brewing route, and it is gated ----------
def test_start_requires_the_acknowledgement(server):
    status, data = post(server, "/api/start", {"params": {"dose": 15}, "name": "Test"})
    assert status == 400 and "acknowledgement" in data["error"]
    assert server.launched == []


def test_start_requires_a_name(server):
    status, data = post(server, "/api/start",
                        {"params": {"dose": 15}, "acknowledge": True, "name": "  "})
    assert status == 400 and "name this brew" in data["error"]
    assert server.launched == []


def test_start_rejects_a_truthy_non_true_acknowledgement(server):
    """`is not True` and not a truthiness test: "no" and 0 must not consent."""
    for ack in ["yes", 1, "true", [1]]:
        status, _ = post(server, "/api/start",
                         {"params": {"dose": 15}, "acknowledge": ack, "name": "Test"})
        assert status == 400
    assert server.launched == []


def test_start_with_the_full_gate_brews(server):
    status, data = post(server, "/api/start",
                        {"params": {"dose": 15}, "acknowledge": True, "name": "Sunday Filter"})
    assert status == 200
    call = server.launched[0]
    assert call["start"] is True and "--start" in call["argv"]
    assert call["name"] == "Sunday Filter"
    assert data["run"]["mode"] == "load + START"
    # The name reached the recipe the machine was handed.
    assert brewgen.cached_name(call["path"]) == "Sunday Filter"


def test_a_run_carries_the_recipe_it_brewed(server):
    """The page's "what is brewing" card comes from the run, read from the file the
    machine was handed — so staging another recipe afterwards cannot change it."""
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0, pours=2), server.root)
    brewgen.set_color(path, "#336699")
    status, data = post(server, "/api/start",
                        {"file": path.name, "acknowledge": True, "name": "Morning"})
    assert status == 200
    details = data["run"]["details"]
    assert details["name"] == "Morning" and details["color"] == "#336699"
    assert "15 g" in details["summary"]
    assert [p["pause_s"] for p in details["pours"]] == [50, 15, 19]
    assert get(server, "/api/run")[1]["run"]["details"] == details


def test_brewing_the_custom_recipe_does_not_make_a_saved_recipe(server):
    """A one-off brew is not a recipe: nothing appears in the saved list, nothing is
    marked as brewed, and the file the machine was given is out of the cache listing."""
    post(server, "/api/start",
         {"params": {"dose": 15}, "acknowledge": True, "name": "One-off"})
    call = server.launched[0]
    assert call["path"].parent.name == brewgen.SCRATCH_DIR and call["path"].exists()
    assert list(brewgen.cache_dir(server.root).glob("*.yaml")) == []
    assert get(server, "/api/recent")[1]["brews"] == []
    assert brewgen._ledger(server.root) == {}


def test_a_second_custom_brew_reuses_the_scratch_file(server):
    for dose in (12, 15):
        post(server, "/api/start",
             {"params": {"dose": dose}, "acknowledge": True, "name": "One-off"})
    assert server.launched[0]["path"] == server.launched[1]["path"]
    assert brewgen.params_from_yaml(server.launched[1]["path"].read_text()).dose == 15.0


def test_the_scratch_file_cannot_be_named_through_the_api(server):
    post(server, "/api/start",
         {"params": {"dose": 15}, "acknowledge": True, "name": "One-off"})
    for route in ("/api/load", "/api/delete", "/api/color", "/api/rename"):
        status, _ = post(server, route, {"file": ".scratch/custom-brew.yaml", "name": "x",
                                         "color": "#112233"})
        assert status == 400, route
    assert get(server, "/api/recipe?file=.scratch/custom-brew.yaml")[0] == 400


def test_brewing_a_saved_recipe_still_marks_it_brewed(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    post(server, "/api/start", {"file": path.name, "acknowledge": True, "name": path.stem})
    assert get(server, "/api/recent")[1]["brews"][0]["brewed"] is True


# --- SAFETY: a cross-origin page cannot reach the brewing routes ------------
@pytest.mark.parametrize("route", ["/api/load", "/api/start", "/api/run/stop"])
def test_posts_require_the_local_header(server, route):
    """Without the custom header (which a cross-origin fetch cannot set without a
    preflight this server never grants), a random website on your loopback gets nothing."""
    status, data = post(server, route, {"params": {"dose": 15}}, local=False)
    assert status == 403 and webui.LOCAL_HEADER in data["error"]
    assert server.launched == []


def test_post_with_a_foreign_origin_is_refused(server):
    req = urllib.request.Request(
        server.base + "/api/load", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", webui.LOCAL_HEADER: "1",
                 "Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403
    assert server.launched == []


# --- re-brewing a cached file -----------------------------------------------
def test_rebrew_loads_the_file_as_it_is_on_disk(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    path.write_text(path.read_text(encoding="utf-8") + "# hand-edited\n", encoding="utf-8")
    edited = path.read_text(encoding="utf-8")
    status, _ = post(server, "/api/load", {"file": path.name})
    assert status == 200
    assert server.launched[0]["path"] == path
    assert path.read_text(encoding="utf-8") == edited      # not regenerated over
    assert server.launched[0]["start"] is False


def test_rebrew_rejects_path_traversal(server):
    outside = server.root.parent / "outside.yaml"
    outside.write_text("name: nope\n", encoding="utf-8")
    status, data = post(server, "/api/load", {"file": "../outside.yaml"})
    assert status == 400 and "not a cache filename" in data["error"]
    assert server.launched == []


def test_rebrew_refuses_an_unreadable_cached_file(server):
    (server.root / "broken.yaml").write_text("name: nope\n", encoding="utf-8")
    status, data = post(server, "/api/load", {"file": "broken.yaml"})
    assert status == 400 and "will not load" in data["error"]
    assert server.launched == []


# --- one run at a time ------------------------------------------------------
def test_a_second_run_while_one_is_live_is_refused(tmp_path, monkeypatch):
    """The real Runner (not the fixture's stub): one machine, one BLE link, one brew."""
    runner = webui.Runner()
    runner._run = webui.Run(id=1, argv=[], recipe="live", file="a.yaml",
                            started_brew=False, started_at=0.0)   # returncode None → live
    with pytest.raises(RuntimeError, match="already running"):
        runner.launch(tmp_path / "r.yaml", start=False, name="x", cwd=tmp_path,
                      address=None, timeout=1.0, debug=False)


def test_stop_reports_when_there_is_nothing_to_stop():
    assert webui.Runner().stop() is False


def test_run_json_paginates_the_log():
    run = webui.Run(id=1, argv=[], recipe="r", file="f.yaml", started_brew=False,
                    started_at=0.0)
    run.lines = ["one", "two", "three"]
    assert run.to_json()["lines"] == ["one", "two", "three"]
    assert run.to_json()["next"] == 3
    assert run.to_json(since=2)["lines"] == ["three"]


# --- staging: read a saved recipe back, edit it, fork it, delete it ----------
def test_recipe_reads_a_cached_brew_back_as_parameters(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0, temp=88), server.root)
    status, data = get(server, "/api/recipe?file=" + path.name)
    assert status == 200 and data["editable"] is True
    assert data["params"]["dose"] == 15.0 and data["params"]["temp"] == 88
    assert data["fingerprint"] == brewgen.fingerprint(path.read_text(encoding="utf-8"))


def test_recipe_marks_a_hand_written_file_as_not_editable(server):
    path = brewgen.cache_dir(server.root)
    path.mkdir(parents=True, exist_ok=True)
    (path / "2000-01-01-hand.yaml").write_text(
        "name: Hand\ndose_g: 10\ngrind: 0\nratio: 17\npours:\n"
        "  - {label: Bloom, ml: 29, temp_c: 91, pattern: spiral, pause_s: 50, rpm: 60, "
        "flow_ml_s: 3.5, agitation: false, vibrate_before: false}\n", encoding="utf-8")
    status, data = get(server, "/api/recipe?file=2000-01-01-hand.yaml")
    assert status == 200 and data["editable"] is False and data["params"] is None


def test_save_rewrites_the_staged_file_without_moving_it(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, data = post(server, "/api/save",
                        {"file": path.name, "params": {"dose": 15, "temp": 85}})
    assert status == 200 and data["file"] == path.name
    assert brewgen.params_from_yaml(path.read_text(encoding="utf-8")).temp == 85
    assert server.launched == []            # saving a file is not brewing one


def test_save_refuses_an_invalid_recipe(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    before = path.read_text(encoding="utf-8")
    status, data = post(server, "/api/save", {"file": path.name, "params": {"dose": 0}})
    assert status == 400 and "not saved" in data["error"]
    assert path.read_text(encoding="utf-8") == before


def test_fork_makes_a_second_file_and_leaves_the_first_alone(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, data = post(server, "/api/fork", {"params": {"dose": 15}})
    assert status == 200 and data["file"] != path.name
    assert data["name"].endswith("copy")
    assert path.exists()
    assert len(list(brewgen.cache_dir(server.root).glob("*.yaml"))) == 2
    assert server.launched == []


def test_delete_removes_one_cached_recipe(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, data = post(server, "/api/delete", {"file": path.name})
    assert status == 200 and data["deleted"] == path.name
    assert not path.exists()
    assert server.launched == []


def test_save_fork_and_delete_need_the_local_header(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    for route, body in (("/api/save", {"file": path.name, "params": {"dose": 12}}),
                        ("/api/fork", {"params": {"dose": 12}}),
                        ("/api/delete", {"file": path.name}),
                        ("/api/rename", {"file": path.name, "name": "X"})):
        status, _ = post(server, route, body, local=False)
        assert status == 403, route
    assert path.exists() and len(list(brewgen.cache_dir(server.root).glob("*.yaml"))) == 1


def test_delete_cannot_escape_the_cache_directory(server):
    outside = server.root / "secret.yaml"
    outside.write_text("name: nope\n", encoding="utf-8")
    status, _ = post(server, "/api/delete", {"file": "../secret.yaml"})
    assert status == 400 and outside.exists()


def test_only_a_start_marks_a_brew_as_brewed(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    post(server, "/api/load", {"file": path.name})
    post(server, "/api/save", {"file": path.name, "params": {"dose": 15, "temp": 85}})
    assert get(server, "/api/recent")[1]["brews"][0]["brewed"] is False
    status, _ = post(server, "/api/start", {"file": path.name, "acknowledge": True,
                                            "name": "Mine"})
    assert status == 200
    assert get(server, "/api/recent")[1]["brews"][0]["brewed"] is True


def test_color_sets_a_recipe_colour_without_brewing(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, data = post(server, "/api/color", {"file": path.name, "color": "#3366AA"})
    assert status == 200 and data["color"] == "#3366aa"
    assert get(server, "/api/recent")[1]["brews"][0]["color"] == "#3366aa"
    assert get(server, "/api/recipe?file=" + path.name)[1]["color"] == "#3366aa"
    assert server.launched == []


def test_there_is_no_clearing_a_colour(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    post(server, "/api/color", {"file": path.name, "color": "#3366aa"})
    status, data = post(server, "/api/color", {"file": path.name, "color": ""})
    assert status == 400 and "colour" in data["error"]
    assert get(server, "/api/recent")[1]["brews"][0]["color"] == "#3366aa"


def test_a_recipe_with_no_colour_is_given_one_and_keeps_it(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    assert brewgen.cached_color(path.read_text(encoding="utf-8")) is None
    first = get(server, "/api/recent")[1]["brews"][0]["color"]
    assert first and first.startswith("#") and len(first) == 7
    assert get(server, "/api/recent")[1]["brews"][0]["color"] == first   # not re-rolled
    assert brewgen.cached_color(path.read_text(encoding="utf-8")) == first


def test_color_rejects_a_bad_value_and_needs_the_local_header(server):
    path, _ = brewgen.write_cached(brewgen.BrewParams(dose=15.0), server.root)
    status, _ = post(server, "/api/color", {"file": path.name, "color": "red"})
    assert status == 400
    status, _ = post(server, "/api/color", {"file": path.name, "color": "#112233"},
                     local=False)
    assert status == 403


def test_create_saves_a_named_recipe_and_spawns_nothing(server):
    status, data = post(server, "/api/create",
                        {"params": {"dose": 15, "temp": 88}, "name": "Sunday Long"})
    assert status == 200 and data["name"] == "Sunday Long" and data["color"]
    got = get(server, "/api/recipe?file=" + data["file"])[1]
    assert got["params"]["temp"] == 88 and got["editable"] is True
    assert server.launched == []


def test_create_refuses_an_invalid_recipe_or_a_missing_name(server):
    assert post(server, "/api/create", {"params": {"dose": 0}, "name": "X"})[0] == 400
    assert post(server, "/api/create", {"params": {"dose": 15}, "name": " "})[0] == 400
    assert post(server, "/api/create", {"params": {}, "name": "X"}, local=False)[0] == 403
    assert list(brewgen.cache_dir(server.root).glob("*.yaml")) == []


def test_opening_a_recipe_saved_under_the_old_rules_migrates_it(server):
    """It is carried forward when opened: the page gets parameters for it and the file
    is rewritten to match, so what is shown and what brews are the same thing."""
    path, _ = brewgen.write_cached(brewgen.BrewParams(), server.root)
    old = path.read_text(encoding="utf-8").replace("temp_c: 80", "temp_c: 91") \
        .replace(" bloom_temp=80", "")
    path.write_text(old, encoding="utf-8")
    assert brewgen.params_from_yaml(old) is None
    got = get(server, "/api/recipe?file=" + path.name)[1]
    assert got["editable"] is True and got["params"]["bloom_temp"] == 91
    assert brewgen.params_from_yaml(path.read_text(encoding="utf-8")) is not None

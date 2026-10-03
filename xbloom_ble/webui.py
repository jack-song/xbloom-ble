"""A small local web UI for the recipe generator.

``xbloom web`` serves one page on ``127.0.0.1`` that shows the brew parameters, the
pours they produce and the live validation, and hands the result to ``xbloom brew``.
It is a *front-end only*: every number, every rule and the command itself come from
:mod:`xbloom_ble.brewgen`, so the page and the terminal guide cannot disagree.

Deliberately stdlib-only (``http.server`` + a static page) — this package stays
dependency-light, and a local single-user tool does not need a framework.

Safety, mirroring CLAUDE.md's invariant:

* ``POST /api/load`` builds a **load-only** command. It cannot start a brew: it never
  passes ``start=True`` to :func:`brewgen.run_brew`, and ``tests/test_webui.py`` asserts
  the resulting argv carries no ``--start``.
* ``POST /api/start`` is the only route that starts one. It requires an explicit
  ``acknowledge: true`` plus a name for the brew — the browser sends those only from the
  confirmation dialog, which is the web equivalent of the terminal guide's TTY gate.
* ``POST /api/rename``, ``/api/save``, ``/api/fork`` and ``/api/delete`` only touch
  files in the recipe cache. None of them spawns anything, and none can reach the
  machine. The page autosaves through ``/api/save`` on every parameter edit, which is
  why it has to stay that way.
* ``POST /api/load`` has **no button on the page** any more — the UI offers staging (a
  page-level idea: which recipe you are editing) and starting, and nothing in between.
  The route stays because loading-without-starting is the safer half of the protocol
  and other clients use it; the test that it never passes ``--start`` stays with it.
* All POST routes require an ``X-XBloom-Local: 1`` header, which a cross-origin page
  cannot set without a preflight this server declines. That keeps some random website
  you have open from POSTing to your loopback port and dispensing hot water.
"""

from __future__ import annotations

import importlib.resources
import json
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import brewgen
from .brewgen import BrewParams, ParamError, StartRefused

LOCAL_HEADER = "X-XBloom-Local"
MAX_BODY = 64 * 1024            # a parameter set is a few hundred bytes; cap the rest
MAX_LOG_LINES = 2000


@dataclass
class Run:
    """One ``xbloom brew`` subprocess and everything the page shows about it."""

    id: int
    argv: list[str]
    recipe: str                 # the recipe name
    file: str                   # the cache filename it was loaded from
    started_brew: bool          # True when this run also sent commit+start (0x42/0x46)
    started_at: float
    lines: list[str] = field(default_factory=list)
    returncode: int | None = None
    proc: subprocess.Popen | None = None

    @property
    def done(self) -> bool:
        return self.returncode is not None

    def to_json(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "recipe": self.recipe,
            "file": self.file,
            "started_brew": self.started_brew,
            "mode": "load + START" if self.started_brew else "load only",
            "elapsed": round(time.time() - self.started_at, 1),
            "done": self.done,
            "returncode": self.returncode,
            "argv": self.argv,
            "lines": self.lines[since:],
            "next": len(self.lines),
        }


class Runner:
    """Holds the one active run. One machine, one BLE link, one brew at a time — so a
    second request while a run is live is refused rather than queued or raced."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: Run | None = None
        self._next_id = 1

    @property
    def current(self) -> Run | None:
        with self._lock:
            return self._run

    def busy(self) -> bool:
        run = self.current
        return run is not None and not run.done

    def launch(self, path: Path, *, start: bool, name: str, cwd: Path,
               address: str | None, timeout: float, debug: bool) -> Run:
        """Start the subprocess. → the :class:`Run`, or raise if one is already live."""
        with self._lock:
            if self._run is not None and not self._run.done:
                raise RuntimeError("a brew is already running — wait for it to finish")
            # run_brew() is still the single hardware entry point: it builds the argv and
            # applies the start guard. `attended` is True only on the /api/start path,
            # where the browser has sent an explicit acknowledgement.
            argv, _ = brewgen.run_brew(
                path, start=start, attended=start, debug=debug, address=address,
                timeout=timeout, dry_run=True,      # dry_run: we spawn it ourselves below
            )
            run = Run(id=self._next_id, argv=argv, recipe=name, file=path.name,
                      started_brew=start, started_at=time.time())
            self._next_id += 1
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            run.proc = proc
            self._run = run
        threading.Thread(target=self._pump, args=(run,), daemon=True).start()
        return run

    def _pump(self, run: Run) -> None:
        """Drain the child's output into ``run.lines`` so the page can poll for it."""
        assert run.proc is not None and run.proc.stdout is not None
        for line in run.proc.stdout:
            with self._lock:
                run.lines.append(line.rstrip("\n"))
                if len(run.lines) > MAX_LOG_LINES:
                    del run.lines[: len(run.lines) - MAX_LOG_LINES]
        run.proc.wait()
        with self._lock:
            run.returncode = run.proc.returncode

    def stop(self) -> bool:
        """Terminate the local subprocess. → False if there was nothing to stop.

        This ends the telemetry stream on *this* machine; it does NOT cancel a brew that
        is already pouring. Cancelling on the machine is ``xbloom``'s own ``0x47`` path
        (``XBloomClient.cancel_brew``), not something a killed watcher does.
        """
        run = self.current
        if run is None or run.done or run.proc is None:
            return False
        run.proc.terminate()
        return True


# --- request handling --------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "xbloom-webui"
    # Set by serve() on the server object; read through self.server.
    protocol_version = "HTTP/1.1"

    # -- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:      # quieter than the default
        if self.server.verbose:                               # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # A local tool's own page; never let anything else frame or embed it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ParamError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ParamError(f"bad JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ParamError("expected a JSON object")
        return data

    def _local_only(self) -> bool:
        """A POST must come from our own page: a custom header (which a cross-origin
        request cannot send without a preflight we never grant) and, when the browser
        sends one at all, an Origin that is this server."""
        if self.headers.get(LOCAL_HEADER) != "1":
            return False
        origin = self.headers.get("Origin")
        if origin:
            host = urlparse(origin).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    # -- routes
    def do_GET(self) -> None:           # noqa: N802 - BaseHTTPRequestHandler's name
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if route.path == "/":
                self._send(200, _page(), "text/html; charset=utf-8")
            elif route.path == "/api/config":
                self._json(self.server.config_json())          # type: ignore[attr-defined]
            elif route.path == "/api/plan":
                self._json(self._plan_from_query(query).to_json())
            elif route.path == "/api/recent":
                root = self.server.root                        # type: ignore[attr-defined]
                self._json({"brews": [b.to_json()
                                      for b in brewgen.recent_brews(root, limit=12)],
                            "dir": str(brewgen.cache_dir(root))})
            elif route.path == "/api/recipe":
                self._json(self._recipe_json((query.get("file") or [""])[0]))
            elif route.path == "/api/run":
                self._json(self._run_json(int((query.get("since") or ["0"])[0])))
            else:
                self._error("not found", 404)
        except ParamError as exc:
            self._error(str(exc))
        except Exception as exc:                               # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:          # noqa: N802
        route = urlparse(self.path)
        if not self._local_only():
            self._error(f"this endpoint only accepts requests from the xbloom page "
                        f"(missing {LOCAL_HEADER} header)", 403)
            return
        try:
            body = self._read_json()
            if route.path == "/api/load":
                self._do_load(body, start=False)
            elif route.path == "/api/start":
                self._do_load(body, start=True)
            elif route.path == "/api/rename":
                self._do_rename(body)
            elif route.path == "/api/save":
                self._do_save(body)
            elif route.path == "/api/fork":
                self._do_fork(body)
            elif route.path == "/api/delete":
                self._do_delete(body)
            elif route.path == "/api/run/stop":
                stopped = self.server.runner.stop()             # type: ignore[attr-defined]
                self._json({"stopped": stopped})
            else:
                self._error("not found", 404)
        except (ParamError, StartRefused) as exc:
            self._error(str(exc))
        except RuntimeError as exc:                # "a brew is already running"
            self._error(str(exc), 409)
        except Exception as exc:                               # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    # -- helpers
    def _plan_from_query(self, query: dict[str, list[str]]) -> brewgen.BrewPlan:
        data = {k: v[0] for k, v in query.items() if k in brewgen.PARAMS_BY_KEY or k == "name"}
        return brewgen.plan(BrewParams.from_mapping(data))

    def _run_json(self, since: int) -> dict[str, Any]:
        run = self.server.runner.current                       # type: ignore[attr-defined]
        if run is None:
            return {"run": None}
        return {"run": run.to_json(since)}

    def _recipe_json(self, filename: str) -> dict[str, Any]:
        """One cached recipe, read back as parameters so the page can stage it.

        ``params`` is None for a recipe this generator cannot reproduce — hand-written,
        or older than the parameter line. Such a file can still be started; it just
        cannot be edited, because editing it would mean regenerating it from a guess.
        """
        srv = self.server                                      # type: ignore[assignment]
        path = brewgen.resolve_cached(filename, srv.root)
        text = path.read_text(encoding="utf-8")
        params = brewgen.params_from_yaml(text)
        summary, ok = brewgen.describe(path)
        return {"file": path.name, "stem": path.stem,
                "name": brewgen.cached_name(path) or path.stem,
                "params": params.as_dict() if params else None,
                "editable": params is not None and ok,
                "readable": ok, "summary": summary,
                "fingerprint": brewgen.fingerprint(text)}

    def _staged_json(self, path: Path) -> dict[str, Any]:
        return {"file": path.name, "stem": path.stem,
                "name": brewgen.cached_name(path) or path.stem}

    def _do_save(self, body: dict[str, Any]) -> None:
        """Autosave the staged recipe. Rewrites one cached file; spawns nothing."""
        srv = self.server                                      # type: ignore[assignment]
        path = brewgen.resolve_cached(str(body.get("file") or ""), srv.root)
        params = BrewParams.from_mapping(body.get("params") or {})
        p = brewgen.plan(params)
        if not p.valid:
            self._error(f"not saved — {p.error}")
            return
        brewgen.save_cached(path, params)
        self._json(self._staged_json(path))

    def _do_fork(self, body: dict[str, Any]) -> None:
        """Copy the current parameters into a new cached recipe, and stage that."""
        srv = self.server                                      # type: ignore[assignment]
        params = BrewParams.from_mapping(body.get("params") or {})
        name = str(body.get("name") or "").strip() or None
        out = brewgen.fork_cached(params, srv.root, name)
        self._json(self._staged_json(out))

    def _do_delete(self, body: dict[str, Any]) -> None:
        """Delete one cached recipe."""
        srv = self.server                                      # type: ignore[assignment]
        path = brewgen.resolve_cached(str(body.get("file") or ""), srv.root)
        brewgen.delete_cached(path, srv.root)
        self._json({"deleted": path.name})

    def _do_rename(self, body: dict[str, Any]) -> None:
        """Rename one cached brew. Touches the label only — never the machine."""
        srv = self.server                                      # type: ignore[assignment]
        path = brewgen.resolve_cached(str(body.get("file") or ""), srv.root)
        out = brewgen.rename_cached(path, str(body.get("name") or ""), srv.root)
        self._json({"file": out.name, "stem": out.stem, "name": brewgen.cached_name(out)})

    def _do_load(self, body: dict[str, Any], *, start: bool) -> None:
        """Load (and on ``/api/start``, start) one recipe.

        Either a parameter set to generate and cache, or ``file``: a cached recipe
        brewed AS IT IS ON DISK — no regeneration, so what ran last time is exactly what
        runs now even if the formulas have since changed.
        """
        srv = self.server                                      # type: ignore[assignment]
        name: str | None = None
        if start:
            # The gate. The page sends these two together from the confirmation dialog
            # and from nowhere else; without them nothing is spawned.
            if body.get("acknowledge") is not True:
                self._error("a remote start needs an explicit acknowledgement that water, "
                            "cup and coffee are in place")
                return
            name = str(body.get("name") or "").strip()
            if not name:
                self._error("name this brew to confirm the start")
                return

        if body.get("file"):
            path = brewgen.resolve_cached(str(body["file"]), srv.root)
            summary, ok = brewgen.describe(path)
            if not ok:
                self._error(f"that cached recipe will not load: {summary}")
                return
            if name:
                path = brewgen.renamed_copy(path, name, srv.root)
            recipe_name = brewgen.cached_name(path) or path.stem
        else:
            params = BrewParams.from_mapping(body.get("params") or {})
            if name:
                # Setting the name feeds both the recipe's `name:` and the cache
                # filename, so the brew is filed under what you just called it. Only set
                # it when it differs from the derived name, or cache_filename would
                # append its slug and "keep the default" would rename the file.
                if name != brewgen.derived_name(params):
                    params.name = name
            p = brewgen.plan(params)
            if not p.valid:
                self._error(f"cannot brew an invalid recipe: {p.error}")
                return
            path, _ = brewgen.write_cached(params, srv.root)
            recipe_name = p.name

        run = srv.runner.launch(path, start=start, name=recipe_name, cwd=srv.cwd,
                                address=srv.address, timeout=srv.timeout, debug=srv.debug)
        if start:
            brewgen.mark_brewed(path, srv.root)      # launch() raised if it did not go
        self._json({"run": run.to_json()})


def _page() -> bytes:
    """The single static page, re-read per request so editing it needs no restart."""
    # Chained single-part joinpath: Traversable.joinpath only takes multiple
    # parts from 3.11, and this package supports 3.10.
    return importlib.resources.files(__package__).joinpath("web").joinpath(
        "index.html").read_bytes()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, root, cwd, address, timeout, debug, verbose):
        super().__init__(addr, handler)
        self.root = root
        self.cwd = cwd
        self.address = address
        self.timeout_s = timeout
        self.debug = debug
        self.verbose = verbose
        self.runner = Runner()

    # `timeout` is taken by socketserver, so the brew timeout gets its own name and a
    # property for the handler to read under the name it thinks in.
    @property
    def timeout(self) -> float:            # type: ignore[override]
        return self.timeout_s

    def config_json(self) -> dict[str, Any]:
        return {
            "address": self.address or "(scan)",
            "timeout": self.timeout_s,
            "debug": self.debug,
            "cache_dir": str(brewgen.cache_dir(self.root)),
            "cwd": str(self.cwd),
            "defaults": BrewParams.defaults().as_dict(),
            "rows": BrewParams.defaults().rows(),
            "max_pour_ml": brewgen.APP_MAX_POUR_ML,
        }


def serve(*, host: str = "127.0.0.1", port: int = 8765, root: str | Path | None = None,
          cwd: Path | None = None, address: str | None = None, timeout: float = 600.0,
          debug: bool = True, open_browser: bool = True, verbose: bool = False) -> int:
    """Run the local web UI until Ctrl-C. → exit code.

    Binds loopback by default and it should stay that way: the page can dispense boiling
    water, so it has no business being reachable from the network.
    """
    httpd = _Server((host, port), _Handler, root=root, cwd=cwd or Path.cwd(),
                    address=address, timeout=timeout, debug=debug, verbose=verbose)
    url = f"http://{host}:{port}/"
    print(f"xBloom web UI → {url}")
    print(f"  recipes cached in {brewgen.cache_dir(root)}")
    print(f"  machine: {address or 'scan on each brew'}    Ctrl-C to stop")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\n⚠️  bound to {host}, not loopback — anyone who can reach this port can "
              f"start a brew.\n")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0

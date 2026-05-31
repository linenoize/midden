"""FastAPI server for the Midden cluster-review UI.

Serves a single static page plus a small JSON API over the Store. Local,
single-user. All mutations go through Store methods (which write reversible
`decisions` rows) — the server holds no business logic of its own.

Run via the CLI:  python -m midden.cli serve --db <path>

Note: signature *computation* (file reads) happens in `midden.cli cluster`, not
here. Boot only materializes clusters from already-stored signatures, so it's
cheap and does no disk I/O over the corpus.
"""
from __future__ import annotations

import json
import string
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import near, topics
from .ingest import ingest as run_ingest
from .store import Store

UI_DIR = Path(__file__).parent / "ui"

# Hosts a same-origin request to a loopback-bound server can legitimately carry.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class KeepBody(BaseModel):
    path_id: int


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="Midden", version="0.1.0")
    store = Store(db_path, same_thread=False)
    # Materialize clusters on boot from whatever is already indexed/signed.
    n_exact = store.materialize_exact_clusters()
    n_doc = near.materialize_doc_versions(store)
    n_img = near.materialize_near_images(store)
    n_topic = topics.materialize_topics(store)  # from persisted topic tags, if any
    app.state.store = store
    app.state.bootstrap = {"exact": n_exact, "doc_version": n_doc,
                           "near_image": n_img, "topic": n_topic}

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Reject cross-origin state-changing requests.

        Several mutation endpoints are bodyless POSTs (purge_all, undo, restore,
        recluster), which browsers treat as "simple" requests — no CORS preflight
        — so any web page the user has open could fire them at the loopback
        server. We block any mutating request whose Origin is not a loopback host.
        Requests with no Origin (CLI/tests/curl) are allowed.
        """
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin:
                host = (urlparse(origin).hostname or "").strip("[]")
                if host not in LOOPBACK_HOSTS:
                    return JSONResponse(
                        {"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.get("/api/overview")
    def overview() -> dict:
        return store.overview()

    @app.post("/api/recluster")
    def recluster() -> dict:
        """Recompute signatures (reads files) and materialize near clusters."""
        result = near.recluster(store)
        result["exact"] = store.materialize_exact_clusters()
        return {**result, **store.overview()}

    @app.get("/api/clusters")
    def clusters(include_resolved: bool = False, kinds: str = "") -> dict:
        # `kinds`: optional comma-separated filter. The review queue passes the
        # dedup kinds; the Projects view passes "topic". Empty = all kinds.
        kind_tuple = tuple(k for k in kinds.split(",") if k) or None
        return {"clusters": store.list_clusters(
            include_resolved=include_resolved, kinds=kind_tuple)}

    @app.get("/api/clusters/{cluster_id}")
    def cluster(cluster_id: int) -> dict:
        c = store.get_cluster(cluster_id)
        if c is None:
            raise HTTPException(404, f"No such cluster: {cluster_id}")
        return c

    @app.post("/api/clusters/{cluster_id}/keep")
    def keep(cluster_id: int, body: KeepBody) -> dict:
        try:
            return store.resolve_keep(cluster_id, body.path_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/clusters/{cluster_id}/purge_all")
    def purge_all(cluster_id: int) -> dict:
        try:
            return store.purge_all(cluster_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/undo")
    def undo() -> dict:
        result = store.undo_last()
        if result is None:
            raise HTTPException(400, "Nothing to undo")
        return result

    @app.get("/api/search")
    def search(q: str, include_purgatory: bool = False) -> dict:
        return {"results": store.search(q, include_purgatory=include_purgatory)}

    @app.get("/api/purgatory")
    def purgatory() -> dict:
        return {"items": store.list_purgatory(), **store.purgatory_summary()}

    @app.post("/api/paths/{path_id}/restore")
    def restore(path_id: int) -> dict:
        try:
            return store.restore_path(path_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/dirs")
    def dirs(path: str = "") -> dict:
        """Read-only directory listing for the folder picker.

        Empty `path` -> the list of existing drive roots (Windows) or `/` (POSIX).
        Returns sub-directories only; symlinks/junctions are skipped (same policy
        as ingest). `parent` is null at the drive-list level, "" at a drive root
        (so "up" returns to the drive list), else the parent path.
        """
        if not path:
            roots = []
            for d in string.ascii_uppercase:
                r = Path(f"{d}:\\")
                if r.exists():
                    roots.append({"name": f"{d}:\\", "path": str(r)})
            if not roots:  # POSIX
                roots.append({"name": "/", "path": "/"})
            return {"path": "", "parent": None, "dirs": roots}

        p = Path(path)
        if not p.is_dir():
            raise HTTPException(400, f"Not a directory: {path}")
        subdirs = []
        try:
            children = sorted(p.iterdir(), key=lambda c: c.name.lower())
        except PermissionError:
            raise HTTPException(403, f"Permission denied: {path}")
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    subdirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # unreadable entry — skip, don't fail the listing
        parent = "" if p.parent == p else str(p.parent)
        return {"path": str(p), "parent": parent, "dirs": subdirs}

    @app.get("/api/ingest/stream")
    def ingest_stream(path: str, label: str = ""):
        """Run ingest over `path`, streaming progress as Server-Sent Events.

        Uses a dedicated Store (its own connection) so the long-running walk never
        shares the request-thread connection. On completion it materializes exact
        clusters (cheap) and emits the fresh overview; near-dup signatures are left
        to the explicit `/api/recluster` (they re-read every file).
        """
        root = Path(path)

        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        def gen():
            job = Store(db_path, same_thread=False)
            n_hashed = n_skipped = n_err = n_symlink = 0
            bytes_hashed = 0
            last = 0.0
            try:
                for ev in run_ingest(root, job, label=label or None):
                    if ev.kind == "started":
                        yield sse({"kind": "started", "path": ev.path})
                    elif ev.kind == "hashed":
                        n_hashed += 1
                        bytes_hashed += ev.size
                        now = time.time()
                        if now - last > 0.1:  # throttle: at most ~10 frames/sec
                            last = now
                            yield sse({"kind": "progress", "hashed": n_hashed,
                                       "skipped": n_skipped, "errors": n_err,
                                       "bytes": bytes_hashed, "path": ev.path})
                    elif ev.kind == "skipped":
                        n_skipped += 1
                    elif ev.kind == "skipped_symlink":
                        n_symlink += 1
                    elif ev.kind == "error":
                        n_err += 1
                        yield sse({"kind": "file_error", "path": ev.path,
                                   "error": ev.error})
                    elif ev.kind == "done":
                        n_clusters = job.materialize_exact_clusters()
                        yield sse({"kind": "done", "hashed": n_hashed,
                                   "skipped": n_skipped, "symlinks": n_symlink,
                                   "errors": n_err, "bytes": bytes_hashed,
                                   "elapsed": ev.elapsed,
                                   "exact_clusters": n_clusters,
                                   "overview": job.overview()})
            except ValueError as e:  # not-a-directory etc. from ingest()
                yield sse({"kind": "error", "error": str(e)})
            except Exception as e:  # noqa: BLE001 — surface anything to the client
                yield sse({"kind": "error", "error": str(e)})
            finally:
                job.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/topics/health")
    def topics_health() -> dict:
        """Which inference backends are usable right now (drives the UI picker)."""
        ok, models = topics.ollama_available()
        return {
            "ollama": {"available": ok, "models": models},
            "anthropic": {"available": topics.anthropic_available()},
            "stub": {"available": True},
            "default": topics.resolve_backend("auto"),
        }

    @app.get("/api/topics/stream")
    def topics_stream(backend: str = "auto", model: str = "", limit: int = 0):
        """Infer topics over untagged docs, streaming progress as SSE.

        Dedicated Store connection (the inference loop is slow — seconds/doc on
        local models — and must not hold the request-thread connection). On
        completion it materializes topic clusters and emits the fresh overview.
        """
        resolved = topics.resolve_backend(backend)

        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        def gen():
            job = Store(db_path, same_thread=False)
            try:
                for ev in topics.compute_topics(
                    job, backend=resolved,
                    model=model or None, limit=limit or None,
                ):
                    if ev["kind"] == "tagged_done":
                        n_clusters = topics.materialize_topics(job)
                        yield sse({"kind": "done", "backend": resolved,
                                   "tagged": ev["tagged"], "skipped": ev["skipped"],
                                   "errors": ev["errors"],
                                   "topic_clusters": n_clusters,
                                   "overview": job.overview()})
                    else:
                        yield sse(ev)
            except Exception as e:  # noqa: BLE001 — surface anything to the client
                yield sse({"kind": "error", "fatal": True, "error": str(e)})
            finally:
                job.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    return app


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8000,
          allow_remote: bool = False) -> None:
    import uvicorn

    # The folder picker (/api/dirs) lists the server's filesystem and the ingest
    # stream (/api/ingest/stream?path=) walks/hashes any server path — both
    # unauthenticated. That's acceptable on loopback (single-user) but a remote
    # exposure on any other interface. Refuse unless the operator opts in.
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise SystemExit(
            f"[midden] refusing to bind {host}: /api/dirs and /api/ingest expose "
            f"the server's filesystem with no auth. Use --host 127.0.0.1, or pass "
            f"--allow-remote if you really intend to serve a non-loopback interface."
        )

    app = create_app(db_path)
    b = app.state.bootstrap
    print(f"[midden] serving {db_path}")
    print(f"[midden] materialized on boot: exact +{b['exact']}, "
          f"doc_version +{b['doc_version']}, near_image +{b['near_image']}, "
          f"topic +{b['topic']}")
    print(f"[midden] ingest a folder from the UI (Ingest tab) or `python -m midden.cli ingest <dir>`")
    print(f"[midden] open http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")

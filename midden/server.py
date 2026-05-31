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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import near
from .ingest import ingest as run_ingest
from .store import Store

UI_DIR = Path(__file__).parent / "ui"


class KeepBody(BaseModel):
    path_id: int


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="Midden", version="0.1.0")
    store = Store(db_path, same_thread=False)
    # Materialize clusters on boot from whatever is already indexed/signed.
    n_exact = store.materialize_exact_clusters()
    n_doc = near.materialize_doc_versions(store)
    n_img = near.materialize_near_images(store)
    app.state.store = store
    app.state.bootstrap = {"exact": n_exact, "doc_version": n_doc, "near_image": n_img}

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
    def clusters(include_resolved: bool = False) -> dict:
        return {"clusters": store.list_clusters(include_resolved=include_resolved)}

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

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    return app


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    app = create_app(db_path)
    b = app.state.bootstrap
    print(f"[midden] serving {db_path}")
    print(f"[midden] materialized on boot: exact +{b['exact']}, "
          f"doc_version +{b['doc_version']}, near_image +{b['near_image']}")
    print(f"[midden] ingest a folder from the UI (Ingest tab) or `python -m midden.cli ingest <dir>`")
    print(f"[midden] open http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")

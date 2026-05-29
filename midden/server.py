"""FastAPI server for the Midden cluster-review UI.

Serves a single static page plus a small JSON API over the Store. Local,
single-user. All mutations go through Store methods (which write reversible
`decisions` rows) — the server holds no business logic of its own.

Run via the CLI:  python -m midden.cli serve --db <path>
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .store import Store

UI_DIR = Path(__file__).parent / "ui"


class KeepBody(BaseModel):
    path_id: int


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="Midden", version="0.1.0")
    store = Store(db_path, same_thread=False)
    # Materialize exact-dup clusters on boot so the queue is populated.
    created = store.materialize_exact_clusters()
    app.state.store = store
    app.state.bootstrap_clusters = created

    @app.get("/api/overview")
    def overview() -> dict:
        return store.overview()

    @app.post("/api/materialize")
    def materialize() -> dict:
        n = store.materialize_exact_clusters()
        return {"created": n, **store.overview()}

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

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    return app


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    app = create_app(db_path)
    n = app.state.bootstrap_clusters
    print(f"[midden] serving {db_path}")
    print(f"[midden] materialized {n} new exact-dup cluster(s) on boot")
    print(f"[midden] open http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")

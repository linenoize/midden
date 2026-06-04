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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import archives, near, organize, topics
from .ingest import ingest as run_ingest
from .store import Store

UI_DIR = Path(__file__).parent / "ui"

# Hosts a same-origin request to a loopback-bound server can legitimately carry.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

MAX_DESTINATIONS = 5


class MoveDest(BaseModel):
    label: Optional[str] = None
    path: str


class KeepBody(BaseModel):
    path_id: int
    # Optional: also queue the kept file to be moved to this destination during
    # the `process` step (organize.py). Index-only + reversible until processed.
    move_dest: Optional[MoveDest] = None


class SettingsBody(BaseModel):
    destinations: list[MoveDest] = []
    holding_dir: Optional[str] = None
    highlight_patterns: list[str] = []


def create_app(db_path: Path, materialize: bool = True) -> FastAPI:
    store = Store(db_path, same_thread=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Materialize clusters from already-stored signatures, on startup.

        Runs inside the lifespan (not at create_app time) so uvicorn binds the
        port *first* — the server is reachable immediately and a quick restart
        doesn't race the previous instance's socket. Materialized clusters are
        persisted in the DB, so `--skip-materialize` safely reuses the last run's
        results when the index hasn't changed since.
        """
        if not materialize:
            app.state.bootstrap = {"exact": 0, "doc_version": 0, "near_image": 0,
                                   "topic": 0, "skipped": True}
            print("[midden] skipped boot materialization (--skip-materialize); "
                  "reusing clusters already in the DB")
        else:
            n_exact = store.materialize_exact_clusters()
            n_doc = near.materialize_doc_versions(store)
            n_img = near.materialize_near_images(store)["created"]
            n_topic = topics.materialize_topics(store)  # from persisted tags, if any
            app.state.bootstrap = {"exact": n_exact, "doc_version": n_doc,
                                   "near_image": n_img, "topic": n_topic}
            print(f"[midden] materialized on boot: exact +{n_exact}, "
                  f"doc_version +{n_doc}, near_image +{n_img}, topic +{n_topic}")
        yield
        store.close()

    app = FastAPI(title="Midden", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.bootstrap = None

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Reject *genuinely* cross-origin state-changing requests.

        Several mutation endpoints are bodyless POSTs (purge_all, undo, restore,
        recluster), which browsers treat as "simple" requests — no CORS preflight
        — so any web page the user has open could fire them at the server. We
        block a mutating request only when its Origin host differs from the host
        the page was actually served from (the request's own Host header).

        Comparing against Host (not a fixed loopback allowlist) is what makes
        same-origin work under `serve --allow-remote`: a UI loaded from
        http://192.168.1.214:8765 sends `Origin: http://192.168.1.214:8765`, whose
        host matches `Host: 192.168.1.214:8765` — same origin, allowed. Loopback
        is always treated as same-origin; requests with no Origin
        (CLI/tests/curl) are allowed.
        """
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin:
                origin_host = (urlparse(origin).hostname or "").strip("[]")
                # Hostname the page was served from (Host header, port stripped).
                served_host = (urlparse(
                    f"//{request.headers.get('host', '')}").hostname or "").strip("[]")
                same_origin = (
                    origin_host == served_host or origin_host in LOOPBACK_HOSTS)
                if not same_origin:
                    return JSONResponse(
                        {"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.get("/api/overview")
    def overview() -> dict:
        return store.overview()

    @app.post("/api/recluster")
    def recluster(reset_near: bool = False, recompute_images: bool = False) -> dict:
        """Recompute signatures (reads files) and materialize near clusters.

        reset_near rebuilds near_image clusters from scratch (repair after the
        clustering fix); recompute_images re-reads images to backfill dimensions.
        """
        result = near.recluster(store, reset_near=reset_near,
                                recompute_images=recompute_images)
        result["exact"] = store.materialize_exact_clusters()
        return {**result, **store.overview()}

    @app.get("/api/clusters")
    def clusters(include_resolved: bool = False, kinds: str = "",
                 min_reclaimable: int = 0) -> dict:
        # `kinds`: optional comma-separated filter. The review queue passes the
        # dedup kinds; the Projects view passes "topic". Empty = all kinds.
        # `min_reclaimable`: hide low-value exact groups (the review queue passes
        # the floor; 0 shows everything).
        kind_tuple = tuple(k for k in kinds.split(",") if k) or None
        return {"clusters": store.list_clusters(
            include_resolved=include_resolved, kinds=kind_tuple,
            min_reclaimable=min_reclaimable)}

    @app.get("/api/clusters/{cluster_id}")
    def cluster(cluster_id: int) -> dict:
        c = store.get_cluster(cluster_id)
        if c is None:
            raise HTTPException(404, f"No such cluster: {cluster_id}")
        return c

    @app.post("/api/clusters/{cluster_id}/keep")
    def keep(cluster_id: int, body: KeepBody) -> dict:
        dest = ({"label": body.move_dest.label, "path": body.move_dest.path}
                if body.move_dest else None)
        try:
            return store.resolve_keep(cluster_id, body.path_id, move_dest=dest)
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

    @app.get("/api/thumb/{hash}")
    def thumb(hash: str):
        """Downscaled JPEG of one active copy of `hash`, for the image review.
        Path is resolved strictly from the DB (never client-supplied); any
        non-image / unreadable source returns 404 so the UI falls back to text."""
        import io

        from . import phash
        if not phash.PIL_AVAILABLE:
            raise HTTPException(404, "thumbnails unavailable (Pillow not installed)")
        src = store.thumb_source(hash)
        if not src:
            raise HTTPException(404, "no active source for this hash")
        from PIL import Image
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((160, 160))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=80)
        except Exception:
            raise HTTPException(404, "not a renderable image")
        return Response(content=buf.getvalue(), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/purgatory")
    def purgatory() -> dict:
        return {"items": store.list_purgatory(), **store.purgatory_summary()}

    @app.post("/api/paths/{path_id}/restore")
    def restore(path_id: int) -> dict:
        try:
            return store.restore_path(path_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---- organize: settings, queues, and the destructive process step ----
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {
            "destinations": store.get_destinations(),
            "holding_dir": store.get_holding_dir(),
            "highlight_patterns": store.get_highlight_patterns(),
            "max_destinations": MAX_DESTINATIONS,
        }

    @app.post("/api/settings")
    def set_settings(body: SettingsBody) -> dict:
        if len(body.destinations) > MAX_DESTINATIONS:
            raise HTTPException(400, f"at most {MAX_DESTINATIONS} destinations")
        dests = [{"label": d.label, "path": d.path} for d in body.destinations if d.path]
        store.set_destinations(dests)
        store.set_holding_dir(body.holding_dir or None)
        store.set_highlight_patterns([p for p in body.highlight_patterns if p.strip()])
        # Register destinations + holding as managed drives now, so ingest/reconcile
        # skip them immediately (before any file is ever moved there).
        for d in dests:
            store.ensure_managed_drive(d["path"], d["label"])
        if body.holding_dir:
            store.ensure_managed_drive(body.holding_dir, "holding")
        return get_settings()

    @app.get("/api/pending")
    def pending() -> dict:
        keepers = store.list_pending(kind="keeper", statuses=("pending",))
        dups = store.dup_candidates()
        return {
            "keepers": [{"path_id": k["path_id"], "rel": k["rel"], "size": k["size"],
                         "dest_label": k["dest_label"], "dest_root": k["dest_root"]}
                        for k in keepers],
            "dups": [{"path_id": d["path_id"], "rel": d["rel"], "size": d["size"]}
                     for d in dups],
            "keeper_bytes": sum(k["size"] or 0 for k in keepers),
            "dup_bytes": sum(d["size"] or 0 for d in dups),
            "holding_dir": store.get_holding_dir(),
        }

    @app.post("/api/process/undo")
    def process_undo() -> dict:
        try:
            result = organize.undo_last_process(store)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        if result is None:
            raise HTTPException(400, "no processed relocation to undo")
        return {**result, **store.overview()}

    @app.get("/api/process/stream")
    def process_stream(dry_run: bool = True, verify: bool = True):
        """Execute (or preview) the queued relocations, streaming SSE progress.

        Dedicated Store connection (the move loop is slow and must not hold the
        request-thread connection). dry_run=True (the default) previews only.
        """
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        def gen():
            job = Store(db_path, same_thread=False)
            try:
                for ev in organize.process(job, dry_run=dry_run, verify=verify):
                    if ev.get("kind") == "done":
                        yield sse({**ev, "overview": job.overview()})
                    else:
                        yield sse(ev)
            except Exception as e:  # noqa: BLE001 — surface anything to the client
                yield sse({"kind": "error", "fatal": True, "error": str(e)})
            finally:
                job.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/archive/{path_id}")
    def archive(path_id: int) -> dict:
        """List the contents of an archive at a specific path (read-only, never
        extracts). Lets the reviewer confirm what's inside a duplicate archive
        when the outer filenames differ. Path resolved strictly from the DB."""
        src = store.path_abspath(path_id)
        if not src:
            raise HTTPException(404, f"No such path: {path_id}")
        if not archives.is_archive(src):
            raise HTTPException(400, "not a recognized archive")
        return {"path_id": path_id, **archives.list_archive(Path(src))}

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
            n_hashed = n_skipped = n_err = n_symlink = n_short = 0
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
                    elif ev.kind == "short_read":
                        n_short += 1
                        yield sse({"kind": "short_read", "path": ev.path,
                                   "error": ev.error})
                    elif ev.kind == "error":
                        n_err += 1
                        yield sse({"kind": "file_error", "path": ev.path,
                                   "error": ev.error})
                    elif ev.kind == "done":
                        n_clusters = job.materialize_exact_clusters()
                        yield sse({"kind": "done", "hashed": n_hashed,
                                   "skipped": n_skipped, "symlinks": n_symlink,
                                   "short_read": n_short,
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
        ok, models = topics.llm_available()
        return {
            "local": {"available": ok, "models": models,
                      "base": topics.llm_base()},
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
          allow_remote: bool = False, materialize: bool = True) -> None:
    import os
    import uvicorn

    from . import env

    # Require the LLM API key before launching. It lives in the gitignored .env at
    # the repo root (populated into the environment on `import midden`); we reload
    # here so a freshly-edited .env is picked up without re-importing. Refusing to
    # start without it is a deliberate product decision — topic inference is a core
    # feature and a silently keyless server is a confusing half-broken state.
    env.load_dotenv()
    if not os.environ.get("MIDDEN_LLM_API_KEY"):
        raise SystemExit(
            f"[midden] MIDDEN_LLM_API_KEY is not set. Copy {env.REPO_ROOT / '.env.example'} "
            f"to {env.REPO_ROOT / '.env'} and fill in the key (see "
            f"windows-sysadmin/CONNECT.md), or export MIDDEN_LLM_API_KEY in your "
            f"environment, then start `midden serve` again."
        )

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

    app = create_app(db_path, materialize=materialize)
    print(f"[midden] serving {db_path}")
    # Cluster materialization runs in the startup handler (after the port binds)
    # and prints its own summary there.
    print(f"[midden] ingest a folder from the UI (Ingest tab) or `python -m midden.cli ingest <dir>`")
    print(f"[midden] open http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")

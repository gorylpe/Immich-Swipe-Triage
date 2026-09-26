"""Immich triage PoC: file unalbumed photos/videos one by one with the arrow keys.

Backend: serves static/index.html and proxies the Immich API, keeping the API key server-side.
Targets Immich v3.2+ (structured `filter` search shape).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path as PathParam, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
INDEX_HTML = ROOT / "static" / "index.html"

HOST, PORT = "127.0.0.1", 8765
MIN_VERSION = (3, 2, 0)
SEARCH_PAGE_SIZE = 200
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
KEYS = ("left", "right", "up", "down")
DEFAULT_CONFIG: dict[str, Any] = {
    "minAgeDays": 366,
    "bindings": {"left": None, "right": None, "up": {"type": "skip"}, "down": {"type": "trash"}},
}

load_dotenv(ROOT / ".env")
IMMICH_URL = os.environ.get("IMMICH_URL", "").strip().rstrip("/").removesuffix("/api")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "").strip()

client = httpx.AsyncClient(
    base_url=f"{IMMICH_URL}/api",
    headers={"x-api-key": IMMICH_API_KEY, "accept": "application/json"},
    timeout=httpx.Timeout(30.0, connect=10.0),
    follow_redirects=True,  # /thumbnail?size=fullsize may redirect to /original or ?size=preview
)
app = FastAPI(title="Immich Swipe Triage")
_me: dict[str, Any] | None = None


# ---------------------------------------------------------------- Immich helpers


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
        msg = body.get("message") or body.get("error") or r.text
        if isinstance(msg, list):
            msg = "; ".join(map(str, msg))
    except ValueError:
        msg = r.text
    return f"Immich {r.status_code}: {str(msg)[:300]}"


async def immich(method: str, path: str, **kwargs) -> Any:
    try:
        r = await client.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Cannot reach Immich: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise HTTPException(502, _error_message(r))
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


async def me() -> dict[str, Any]:
    global _me
    if _me is None:
        _me = await immich("GET", "/users/me")
    return _me


def base_filter(cutoff: str) -> dict[str, Any]:
    # The v3 filter shape has no implicit "not trashed" default and returns hidden
    # (live-photo motion parts) and archived assets unless we say otherwise.
    return {
        "hasAlbums": {"eq": False},
        "trashedAt": {"eq": None},
        "visibility": {"eq": "timeline"},
        "type": {"in": ["IMAGE", "VIDEO"]},
        "takenAt": {"lt": cutoff},
    }


def slim_asset(a: dict[str, Any]) -> dict[str, Any]:
    exif = a.get("exifInfo") or {}
    return {
        "id": a["id"],
        "type": a["type"],
        "originalFileName": a.get("originalFileName"),
        "originalMimeType": a.get("originalMimeType"),
        "fileCreatedAt": a.get("fileCreatedAt"),
        "localDateTime": a.get("localDateTime"),
        "width": exif.get("exifImageWidth") or a.get("width"),
        "height": exif.get("exifImageHeight") or a.get("height"),
        "fileSize": exif.get("fileSizeInByte"),
        "make": exif.get("make"),
        "model": exif.get("model"),
        "lensModel": exif.get("lensModel"),
        "duration": a.get("duration"),  # milliseconds (v3), null for images
    }


# ---------------------------------------------------------------- config


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            cfg["minAgeDays"] = saved.get("minAgeDays", cfg["minAgeDays"])
            cfg["bindings"].update({k: v for k, v in (saved.get("bindings") or {}).items() if k in KEYS})
        except (ValueError, OSError) as e:
            print(f"warning: ignoring unreadable {CONFIG_PATH.name}: {e}", file=sys.stderr)
    return cfg


class Binding(BaseModel):
    type: Literal["album", "trash", "skip"]
    albumId: str | None = Field(default=None, pattern=UUID_RE)
    albumName: str | None = None


class Config(BaseModel):
    minAgeDays: int = Field(ge=0, le=100_000)
    bindings: dict[Literal["left", "right", "up", "down"], Binding | None]


@app.get("/api/config")
async def get_config():
    return load_config()


@app.put("/api/config")
async def put_config(cfg: Config):
    data = cfg.model_dump(exclude_none=False)
    data["bindings"] = {k: (b.model_dump(exclude_none=True) if b else None) for k, b in cfg.bindings.items()}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)
    return data


# ---------------------------------------------------------------- server info & albums


@app.get("/api/info")
async def info():
    v = await immich("GET", "/server/version")
    features = await immich("GET", "/server/features")
    user = await me()
    version = (v["major"], v["minor"], v["patch"])
    return {
        "immichUrl": IMMICH_URL,
        "version": ".".join(map(str, version)),
        "versionOk": version >= MIN_VERSION,
        "minVersion": ".".join(map(str, MIN_VERSION)),
        "trashEnabled": bool(features.get("trash")),
        "user": user.get("name") or user.get("email"),
    }


@app.get("/api/albums")
async def albums():
    data = await immich("GET", "/albums")
    out = [{"id": a["id"], "albumName": a["albumName"], "assetCount": a.get("assetCount", 0)} for a in data]
    return sorted(out, key=lambda a: a["albumName"].casefold())


class NewAlbum(BaseModel):
    albumName: str = Field(min_length=1, max_length=200)


@app.post("/api/albums")
async def create_album(body: NewAlbum):
    a = await immich("POST", "/albums", json={"albumName": body.albumName.strip()})
    return {"id": a["id"], "albumName": a["albumName"], "assetCount": a.get("assetCount", 0)}


# ---------------------------------------------------------------- queue


class QueueRequest(BaseModel):
    cutoff: str  # ISO timestamp: only assets taken before this
    after: str | None = None  # keyset: fileCreatedAt of the last asset already fetched
    exclude: list[str] = []  # ids already fetched whose fileCreatedAt == after
    want: int = Field(default=50, ge=1, le=500)


@app.post("/api/queue")
async def queue(req: QueueRequest):
    """Oldest-first page of unalbumed assets.

    Uses keyset pagination on fileCreatedAt instead of Immich's cursor, because the
    cursor is a plain offset and the result set shrinks as assets get filed/trashed.
    """
    my_id = (await me())["id"]
    after, exclude, cursor = req.after, list(req.exclude), None
    out: list[dict[str, Any]] = []
    done = False

    for _ in range(25):
        flt = base_filter(req.cutoff)
        if after:
            flt["takenAt"]["gte"] = after
        body: dict[str, Any] = {
            "filter": flt,
            "orderBy": {"field": "fileCreatedAt", "direction": "asc"},
            "size": SEARCH_PAGE_SIZE,
            "withExif": True,
        }
        if cursor:
            body["cursor"] = cursor
        res = (await immich("POST", "/search/metadata", json=body))["assets"]
        items = res["items"]
        if not items:
            done = True
            break

        excluded = set(exclude)
        # Partner assets (shared into the timeline) also match; we can't file or trash those.
        out += [slim_asset(a) for a in items if a["id"] not in excluded and a["ownerId"] == my_id]

        last = items[-1]["fileCreatedAt"]
        if last == after:
            # A whole page shares one timestamp: step through it with the offset cursor.
            exclude += [a["id"] for a in items]
            cursor = res.get("nextCursor")
        else:
            after, cursor = last, None
            exclude = [a["id"] for a in items if a["fileCreatedAt"] == last]

        if not res.get("nextCursor"):
            done = True
            break
        if len(out) >= req.want:
            break

    return {"items": out, "after": after, "exclude": exclude, "done": done}


class StatsRequest(BaseModel):
    cutoff: str


@app.post("/api/stats")
async def stats(req: StatsRequest):
    res = await immich("POST", "/search/statistics", json={"filter": base_filter(req.cutoff)})
    return {"total": res["total"]}


# ---------------------------------------------------------------- actions


class ActionRequest(BaseModel):
    op: Literal["album_add", "album_remove", "trash", "restore"]
    assetId: str = Field(pattern=UUID_RE)
    albumId: str | None = Field(default=None, pattern=UUID_RE)


def _check_bulk(results: list[dict[str, Any]], ok_errors: tuple[str, ...] = ()) -> None:
    for r in results or []:
        if not r.get("success") and r.get("error") not in ok_errors:
            raise HTTPException(502, f"Immich: {r.get('errorMessage') or r.get('error') or 'failed'}")


@app.post("/api/action")
async def action(req: ActionRequest):
    ids = {"ids": [req.assetId]}
    if req.op in ("album_add", "album_remove") and not req.albumId:
        raise HTTPException(422, "albumId required")

    if req.op == "album_add":
        _check_bulk(await immich("PUT", f"/albums/{req.albumId}/assets", json=ids), ok_errors=("duplicate",))
    elif req.op == "album_remove":
        _check_bulk(await immich("DELETE", f"/albums/{req.albumId}/assets", json=ids))
    elif req.op == "trash":
        # With trash disabled, "soft" deleted assets are purged by the next nightly cleanup.
        features = await immich("GET", "/server/features")
        if not features.get("trash"):
            raise HTTPException(409, "Trash is disabled on the Immich server; refusing to delete")
        await immich("DELETE", "/assets", json={**ids, "force": False})
    elif req.op == "restore":
        res = await immich("POST", "/trash/restore/assets", json=ids)
        if not res or not res.get("count"):
            raise HTTPException(502, "Immich restored 0 assets (not in trash?)")
    return {"ok": True}


# ---------------------------------------------------------------- media proxy

PASS_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-encoding",
    "etag",
    "last-modified",
    "cache-control",
)


async def stream_media(request: Request, path: str, params: dict[str, str] | None = None):
    headers = {k: request.headers[k] for k in ("range", "if-none-match", "if-modified-since") if k in request.headers}
    try:
        r = await client.send(client.build_request("GET", path, params=params, headers=headers), stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Cannot reach Immich: {e}") from e
    if r.status_code >= 400:
        await r.aread()
        await r.aclose()
        return Response(_error_message(r), status_code=r.status_code, media_type="text/plain")
    return StreamingResponse(
        r.aiter_raw(),
        status_code=r.status_code,
        headers={k: r.headers[k] for k in PASS_HEADERS if k in r.headers},
        background=BackgroundTask(r.aclose),
    )


AssetId = PathParam(pattern=UUID_RE)


@app.get("/media/{asset_id}/original")
async def media_original(request: Request, asset_id: str = AssetId):
    return await stream_media(request, f"/assets/{asset_id}/original")


@app.get("/media/{asset_id}/fullsize")
async def media_fullsize(request: Request, asset_id: str = AssetId):
    return await stream_media(request, f"/assets/{asset_id}/thumbnail", {"size": "fullsize"})


@app.get("/media/{asset_id}/playback")
async def media_playback(request: Request, asset_id: str = AssetId):
    return await stream_media(request, f"/assets/{asset_id}/video/playback")


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML, headers={"cache-control": "no-store"})


if __name__ == "__main__":
    if not IMMICH_URL or not IMMICH_API_KEY:
        sys.exit("Set IMMICH_URL and IMMICH_API_KEY in .env (see .env.example)")
    print(f"Immich: {IMMICH_URL}\nOpen http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

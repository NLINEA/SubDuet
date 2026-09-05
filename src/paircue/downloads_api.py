from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from paircue import __version__
from paircue.config import DownloadStationSettings
from paircue.security import (
    require_bounded_content_length,
    security_headers_middleware,
    token_dependency,
)
from paircue.services.atomic import atomic_write_bytes
from paircue.services.download_station import DownloadStationClient, DownloadStationError


class MagnetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=20, max_length=8192)


class OperationResponse(BaseModel):
    ok: bool
    message: str


class TaskResponse(BaseModel):
    title: str
    status: str


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]


DOWNLOADS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>SubDuet Downloads</title><link rel="stylesheet" href="/assets/downloads.css"></head>
<body><main><h1>SubDuet Downloads</h1>
<label>API token<input id="token" type="password" autocomplete="off"></label>
<label>Magnet link<textarea id="magnet" rows="3"></textarea></label>
<button id="add">Add magnet</button>
<label class="upload">Torrent file<input id="torrent" type="file" accept=".torrent"></label>
<button id="upload">Upload torrent</button><p id="status" aria-live="polite"></p>
<h2>Tasks</h2><button id="refresh">Refresh</button><ul id="tasks"></ul>
</main><script src="/assets/downloads.js" defer></script></body></html>"""

DOWNLOADS_CSS = """body{font:16px system-ui;background:#f5f5f5;color:#1f2937;margin:0}main{max-width:620px;margin:3rem auto;background:white;padding:2rem;border-radius:16px}label{display:block;margin:1rem 0}.upload{margin-top:1.5rem}input,textarea,button{font:inherit}input,textarea{display:block;width:100%;margin-top:.4rem;padding:.7rem;box-sizing:border-box}button{padding:.65rem 1rem;margin:.25rem .35rem .25rem 0}li{padding:.45rem 0;border-bottom:1px solid #eee}#status{min-height:1.5rem}"""

DOWNLOADS_JS = """const q=s=>document.querySelector(s);const auth=()=>({Authorization:`Bearer ${q('#token').value}`});
async function decode(r){const d=await r.json();if(!r.ok)throw new Error(d.detail||'Request failed');return d}
async function tasks(){try{const d=await decode(await fetch('/v1/tasks',{headers:auth()}));const list=q('#tasks');list.replaceChildren();d.tasks.forEach(t=>{const li=document.createElement('li');li.textContent=`${t.status}: ${t.title}`;list.append(li)})}catch(e){q('#status').textContent=e.message}}
q('#refresh').addEventListener('click',tasks);q('#add').addEventListener('click',async()=>{try{const d=await decode(await fetch('/v1/magnets',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({uri:q('#magnet').value})}));q('#status').textContent=d.message;tasks()}catch(e){q('#status').textContent=e.message}});
q('#upload').addEventListener('click',async()=>{const file=q('#torrent').files[0];if(!file)return;q('#status').textContent='Uploading…';try{const form=new FormData();form.append('file',file);const d=await decode(await fetch('/v1/torrents',{method:'POST',headers:auth(),body:form}));q('#status').textContent=d.message;tasks()}catch(e){q('#status').textContent=e.message}});tasks();"""


def create_downloads_app(
    settings: DownloadStationSettings,
    client: DownloadStationClient,
) -> FastAPI:
    app = FastAPI(
        title="SubDuet Download Station",
        version=__version__,
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.middleware("http")(security_headers_middleware)

    @app.middleware("http")
    async def content_security_policy(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return DOWNLOADS_HTML

    @app.get("/assets/downloads.css", response_class=PlainTextResponse)
    async def css() -> PlainTextResponse:
        return PlainTextResponse(DOWNLOADS_CSS, media_type="text/css")

    @app.get("/assets/downloads.js", response_class=PlainTextResponse)
    async def javascript() -> PlainTextResponse:
        return PlainTextResponse(DOWNLOADS_JS, media_type="text/javascript")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "paircue-downloads"}

    require_token = token_dependency(settings.api_token.get_secret_value())
    router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

    @router.get("/tasks", response_model=TaskListResponse)
    async def tasks() -> TaskListResponse:
        try:
            rows = await run_in_threadpool(client.list_tasks)
        except DownloadStationError as exc:
            raise HTTPException(status_code=502, detail="Download Station is unavailable") from exc
        return TaskListResponse(tasks=[TaskResponse(**row) for row in rows])

    @router.post("/magnets", response_model=OperationResponse)
    async def add_magnet(payload: MagnetRequest) -> OperationResponse:
        parsed = urlparse(payload.uri)
        params = parse_qs(parsed.query)
        if parsed.scheme.lower() != "magnet" or not any(
            value.lower().startswith("urn:btih:") for value in params.get("xt", [])
        ):
            raise HTTPException(status_code=422, detail="a BitTorrent magnet link is required")
        try:
            ok = await run_in_threadpool(client.add_magnet, payload.uri)
        except DownloadStationError as exc:
            raise HTTPException(
                status_code=502, detail="Download Station rejected the magnet"
            ) from exc
        return OperationResponse(ok=ok, message="magnet added" if ok else "magnet was not added")

    @router.post("/torrents", response_model=OperationResponse)
    async def upload_torrent(
        request: Request,
        file: Annotated[UploadFile, File(description="A bencoded .torrent file")],
    ) -> OperationResponse:
        require_bounded_content_length(request, settings.max_torrent_bytes + 65536)
        data = await file.read(settings.max_torrent_bytes + 1)
        if len(data) > settings.max_torrent_bytes:
            raise HTTPException(status_code=413, detail="torrent file is too large")
        if not file.filename or not file.filename.lower().endswith(".torrent"):
            raise HTTPException(status_code=422, detail="filename must end with .torrent")
        if not data.startswith(b"d") or not data.endswith(b"e") or b"4:info" not in data:
            raise HTTPException(status_code=422, detail="file does not look like a torrent")
        target = settings.watch_dir / f"{uuid.uuid4().hex}.torrent"
        atomic_write_bytes(target, data, mode=0o640)
        return OperationResponse(ok=True, message="torrent queued")

    app.include_router(router)
    return app

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from paircue import __version__
from paircue.config import PairCueSettings
from paircue.runtime import CoreRuntime
from paircue.security import (
    require_bounded_content_length,
    security_headers_middleware,
    token_dependency,
)


class HealthResponse(BaseModel):
    status: str
    service: str
    platform: str


class QueuedResponse(BaseModel):
    queued: bool
    message: str


class RecentResultResponse(BaseModel):
    media_name: str
    status: str
    message: str
    updated_at: str


class StatusResponse(BaseModel):
    pending: int
    queued: int
    results: dict[str, int]
    recent: list[RecentResultResponse]
    scan_status: str
    scan_message: str


class DashboardContextResponse(BaseModel):
    platform: str
    source_language: str
    target_language: str
    desktop: bool


class DesktopControl(Protocol):
    def request(self, action: Literal["stop", "edit"]) -> None: ...


class PlexMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    rating_key: str = Field(alias="ratingKey", pattern=r"^\d+$")


class PlexWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event: str
    metadata: PlexMetadata = Field(alias="Metadata")


class MediaBrowserWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    notification_type: str = Field(alias="NotificationType")
    item_id: str = Field(alias="ItemId", pattern=r"^[A-Za-z0-9_-]{1,128}$")
    item_type: str = Field(alias="ItemType")


def create_core_app(
    settings: PairCueSettings,
    runtime: CoreRuntime,
    *,
    desktop_control: DesktopControl | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    docs_url = "/docs" if settings.api_docs_enabled else None
    app = FastAPI(
        title="SubDuet API",
        version=__version__,
        debug=False,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.middleware("http")(security_headers_middleware)

    dashboard_root = Path(__file__).with_name("dashboard")
    dashboard_assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
        "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    }

    async def dashboard_asset(request: Request) -> Response:
        filename, media_type = dashboard_assets[request.url.path]
        try:
            content = (dashboard_root / filename).read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=500, detail="dashboard asset is unavailable") from exc
        return Response(content=content, media_type=media_type)

    for dashboard_path in dashboard_assets:
        app.add_api_route(dashboard_path, dashboard_asset, methods=["GET"], include_in_schema=False)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="paircue-core", platform=settings.platform)

    require_token = token_dependency(settings.api_token.get_secret_value())
    protected = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

    @protected.post("/scan", response_model=QueuedResponse)
    async def scan() -> QueuedResponse:
        count = await run_in_threadpool(runtime.scan_now)
        return QueuedResponse(queued=count > 0, message=f"queued {count} item(s)")

    @protected.get("/status", response_model=StatusResponse)
    async def status() -> StatusResponse:
        snapshot = await run_in_threadpool(runtime.status_snapshot)
        return StatusResponse(
            pending=snapshot.pending,
            queued=snapshot.queued,
            results=snapshot.results,
            recent=[
                RecentResultResponse.model_validate(row, from_attributes=True)
                for row in snapshot.recent
            ],
            scan_status=snapshot.scan_status,
            scan_message=snapshot.scan_message,
        )

    @protected.get("/dashboard-context", response_model=DashboardContextResponse)
    async def dashboard_context() -> DashboardContextResponse:
        return DashboardContextResponse(
            platform=settings.platform,
            source_language=settings.source_language,
            target_language=settings.target_language,
            desktop=desktop_control is not None,
        )

    if desktop_control is not None:

        @protected.post("/desktop/stop", response_model=QueuedResponse)
        async def desktop_stop() -> QueuedResponse:
            desktop_control.request("stop")
            return QueuedResponse(queued=False, message="SubDuet is stopping")

        @protected.post("/desktop/edit", response_model=QueuedResponse)
        async def desktop_edit() -> QueuedResponse:
            desktop_control.request("edit")
            return QueuedResponse(queued=False, message="SubDuet is reopening setup")

    @protected.post("/webhooks/plex", response_model=QueuedResponse)
    async def plex_webhook(request: Request) -> QueuedResponse:
        if not settings.webhook_enabled or settings.platform != "plex":
            raise HTTPException(status_code=404, detail="webhook is disabled")
        require_bounded_content_length(request, settings.max_webhook_bytes)
        content_type = request.headers.get("content-type", "").lower()
        try:
            payload: dict[str, Any]
            if content_type.startswith("application/json"):
                payload = json.loads((await request.body()).decode("utf-8"))
            elif "multipart/form-data" in content_type:
                form = await request.form(
                    max_files=1,
                    max_fields=4,
                    max_part_size=settings.max_webhook_bytes,
                )
                raw = form.get("payload")
                if not isinstance(raw, str):
                    raise ValueError("multipart request does not contain a payload field")
                payload = json.loads(raw)
            else:
                raise HTTPException(status_code=415, detail="unsupported content type")
            webhook = PlexWebhook.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid Plex webhook payload") from exc

        if webhook.event != "library.new":
            return QueuedResponse(queued=False, message="event ignored")
        queued = await run_in_threadpool(runtime.submit_rating_key, webhook.metadata.rating_key)
        return QueuedResponse(queued=queued, message="item queued" if queued else "item not found")

    async def media_browser_webhook(
        request: Request, platform: str
    ) -> QueuedResponse:
        if not settings.webhook_enabled or settings.platform != platform:
            raise HTTPException(status_code=404, detail="webhook is disabled")
        require_bounded_content_length(request, settings.max_webhook_bytes)
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise HTTPException(status_code=415, detail="unsupported content type")
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
            webhook = MediaBrowserWebhook.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid {platform} webhook payload"
            ) from exc
        if (
            webhook.notification_type.casefold() != "itemadded"
            or webhook.item_type.casefold() not in {"movie", "episode"}
        ):
            return QueuedResponse(queued=False, message="event ignored")
        queued = await run_in_threadpool(runtime.submit_item_id, webhook.item_id)
        return QueuedResponse(queued=queued, message="item queued" if queued else "item not found")

    @protected.post("/webhooks/jellyfin", response_model=QueuedResponse)
    async def jellyfin_webhook(request: Request) -> QueuedResponse:
        return await media_browser_webhook(request, "jellyfin")

    @protected.post("/webhooks/emby", response_model=QueuedResponse)
    async def emby_webhook(request: Request) -> QueuedResponse:
        return await media_browser_webhook(request, "emby")

    app.include_router(protected)
    return app

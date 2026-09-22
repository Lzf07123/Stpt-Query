"""对外 API、订阅源与后台管理接口。

- `/api/v1/calendars*`：浏览器调用，经 nginx 注入网关令牌（与 /run 同级信任模型）
- `/cal/{token}.ics`：日历客户端直接抓取，**不注入网关令牌**，路径令牌即凭据
- `/admin/api/calendars*`：复用现有 ADMIN_TOKEN 鉴权
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .service import CalendarAuthError, CalendarError, CalendarService

USERNAME = Field(..., min_length=10, max_length=10, pattern=r"^\d{10}$")


class CreateBody(BaseModel):
    username: str = USERNAME
    password: str = Field(..., min_length=1, max_length=256)
    semester: str = Field(..., min_length=4, max_length=16)
    weeks: str = Field(default="all", max_length=64)
    refresh_interval: Optional[int] = Field(default=None, ge=0, le=604800)
    reauthorize: bool = False


class AuthBody(BaseModel):
    username: str = USERNAME
    password: str = Field(..., min_length=1, max_length=256)
    semester: str = Field(..., min_length=4, max_length=16)
    verified_token: Optional[str] = Field(default=None, max_length=256)


def _error(exc: CalendarError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})


def build_router(service: CalendarService, require_auth: Callable[..., Any],
                 require_admin: Callable[..., Any]) -> APIRouter:
    router = APIRouter()

    async def _guard(coro) -> Response:
        try:
            return JSONResponse(content=await coro)
        except CalendarError as exc:
            return _error(exc)

    # ---------------- 公开配置 ----------------
    @router.get("/api/v1/calendars/config", dependencies=[Depends(require_auth)])
    async def calendar_config() -> dict:
        return service.public_config()

    # ---------------- 浏览器 API ----------------
    @router.post("/api/v1/calendars", dependencies=[Depends(require_auth)])
    async def create(body: CreateBody) -> Response:
        return await _guard(service.create(
            body.username, body.password, body.semester, weeks=body.weeks,
            refresh_interval=body.refresh_interval, reauthorize=body.reauthorize))

    @router.post("/api/v1/calendars/status", dependencies=[Depends(require_auth)])
    async def status(body: AuthBody) -> Response:
        return await _guard(service.status(body.username, body.password, body.semester,
                                           body.verified_token))

    @router.post("/api/v1/calendars/refresh", dependencies=[Depends(require_auth)])
    async def refresh(body: AuthBody) -> Response:
        return await _guard(service.refresh(body.username, body.password, body.semester))

    @router.post("/api/v1/calendars/rotate", dependencies=[Depends(require_auth)])
    async def rotate(body: AuthBody) -> Response:
        return await _guard(service.rotate(body.username, body.password, body.semester,
                                           verified_token=body.verified_token))

    @router.delete("/api/v1/calendars", dependencies=[Depends(require_auth)])
    async def close(body: AuthBody) -> Response:
        return await _guard(service.close(body.username, body.password, body.semester,
                                          verified_token=body.verified_token))

    # ---------------- 订阅源（日历客户端直连，无网关令牌） ----------------
    @router.get("/cal/{token}.ics", include_in_schema=False)
    async def feed(request: Request, token: str) -> Response:
        try:
            data = service.feed(token)
        except CalendarError:
            return Response(status_code=404, content=b"not found",
                            media_type="text/plain; charset=utf-8")
        etag = 'W/"%s"' % data["etag"]
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=900, must-revalidate",
            "X-Robots-Tag": "noindex, nofollow",
            "Content-Disposition": 'inline; filename="schedule.ics"',
        }
        if request.headers.get("if-none-match") in (etag, data["etag"]):
            return Response(status_code=304, headers=headers)
        if data.get("last_modified"):
            headers["Last-Modified"] = data["last_modified"]
        return Response(content=data["body"], media_type="text/calendar; charset=utf-8",
                        headers=headers)

    # ---------------- 后台管理 ----------------
    @router.get("/admin/api/calendars", dependencies=[Depends(require_admin)])
    async def admin_list(username: Optional[str] = Query(default=None, min_length=10, max_length=10),
                         semester: Optional[str] = Query(default=None, max_length=16),
                         state: Optional[str] = Query(default=None, max_length=24),
                         page: int = Query(default=1, ge=1, le=1000),
                         size: int = Query(default=20, ge=1, le=100)) -> dict:
        return service.admin_list(username=username, semester=semester, state=state,
                                  page=page, size=size)

    @router.get("/admin/api/calendars/stats", dependencies=[Depends(require_admin)])
    async def admin_stats() -> dict:
        return service.store.stats()

    @router.get("/admin/api/calendars/{calendar_id}", dependencies=[Depends(require_admin)])
    async def admin_detail(calendar_id: str) -> Response:
        record = service.admin_find(calendar_id)
        if record is None:
            return JSONResponse(status_code=404, content={"detail": "订阅不存在"})
        return JSONResponse(content=service.admin_view(record))

    @router.post("/admin/api/calendars/{calendar_id}/refresh", dependencies=[Depends(require_admin)])
    async def admin_refresh(calendar_id: str) -> Response:
        return await _guard(service.admin_refresh(calendar_id))

    @router.post("/admin/api/calendars/{calendar_id}/pause", dependencies=[Depends(require_admin)])
    async def admin_pause(calendar_id: str, hours: int = Query(default=24, ge=1, le=720)) -> Response:
        return await _guard_wrap(service.admin_pause(calendar_id, hours))

    @router.post("/admin/api/calendars/{calendar_id}/resume", dependencies=[Depends(require_admin)])
    async def admin_resume(calendar_id: str) -> Response:
        return await _guard_wrap(service.admin_resume(calendar_id))

    @router.post("/admin/api/calendars/{calendar_id}/rotate", dependencies=[Depends(require_admin)])
    async def admin_rotate(calendar_id: str) -> Response:
        return await _guard_wrap(service.admin_rotate(calendar_id))

    @router.delete("/admin/api/calendars/{calendar_id}", dependencies=[Depends(require_admin)])
    async def admin_delete(calendar_id: str) -> Response:
        return await _guard_wrap(service.admin_delete(calendar_id))

    async def _guard_wrap(value: Any) -> Response:
        try:
            if hasattr(value, "__await__"):
                value = await value
            return JSONResponse(content=value)
        except CalendarError as exc:
            return _error(exc)

    return router

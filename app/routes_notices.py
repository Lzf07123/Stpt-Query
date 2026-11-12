"""全站通知路由：公开 /notices/* 与管理端 /admin/api/notices*。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query

from .context import AppContext
from .notices import NoticeCreate, NoticeError, NoticeUpdate


def _notice_http_error(exc: NoticeError) -> HTTPException:
    """通知存储业务异常 → HTTP 响应的统一包装（单一出口，状态码/详情透传）。"""
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _public_notice(item) -> dict:
    return {
        "id": item.id,
        "content": item.content,
        "level": item.level,
        "published_at": item.published_at,
    }


def register_notice_routes(app, ctx: AppContext) -> None:
    @app.get("/notices/active", dependencies=[Depends(ctx.require_auth)])
    async def active_notices() -> dict:
        try:
            items = await app.state.notice_store.active()
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {"notices": [_public_notice(item) for item in items]}

    @app.get("/notices/history")
    async def notice_history(
        limit: int = Query(default=50, ge=1, le=100),
        _auth: None = Depends(ctx.require_auth),
    ) -> dict:
        try:
            items = await app.state.notice_store.history(limit)
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {
            "notices": [
                {
                    "id": item.id,
                    "content": item.content,
                    "level": item.level,
                    "published_at": item.published_at,
                    "archived_at": item.archived_at,
                }
                for item in items
            ]
        }

    @app.get("/admin/api/notices")
    async def admin_notices(
        _: None = Depends(ctx.require_admin),
        notice_status: str = Query(default="", alias="status", pattern="^(|draft|active|archived)$"),
        keyword: str = Query(default="", max_length=100),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        try:
            items, total = await app.state.notice_store.admin_list(
                notice_status, keyword, offset, limit)
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {
            "notices": [item.model_dump() for item in items],
            "total": total,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < total,
            },
            "storage": {
                "status": app.state.notice_store.status,
                "redis": app.state.notice_store.redis_status,
                "parse_errors": app.state.notice_store.parse_errors,
                "last_error": app.state.notice_store.last_error,
            },
        }

    @app.post("/admin/api/notices")
    async def create_notice(
        payload: NoticeCreate,
        _: None = Depends(ctx.require_admin),
    ) -> dict:
        try:
            item = await app.state.notice_store.create(payload)
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {"notice": item.model_dump()}

    @app.patch("/admin/api/notices/{notice_id}")
    async def update_notice(
        notice_id: str,
        payload: NoticeUpdate,
        _: None = Depends(ctx.require_admin),
    ) -> dict:
        try:
            item = await app.state.notice_store.update(notice_id, payload)
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {"notice": item.model_dump()}

    @app.delete("/admin/api/notices/{notice_id}")
    async def delete_notice(
        notice_id: str,
        _: None = Depends(ctx.require_admin),
    ) -> dict:
        try:
            await app.state.notice_store.delete(notice_id)
        except NoticeError as exc:
            raise _notice_http_error(exc)
        return {"deleted": True}

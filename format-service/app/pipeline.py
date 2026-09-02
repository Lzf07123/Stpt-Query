"""查询编排主流程：把固定的 Dify 工作流图代码化为顺序分支。

对应原图：login → 状态判断 → 获取session → 查询项目分支 → /jump
→ 状态判断 → 渲染/分析/格式化 → (md2pdf 分支) → 合并输出。
迁移时已修复原编排中的两处问题：
1. 不再把登录响应拼入输出（原变量聚合器泄漏 session 的风险）；
2. PDF 分支直接依据结构化 success 字段判断，不再依赖失效的字符串条件。
"""
from __future__ import annotations

import base64
import asyncio
import time
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

import httpx

from .classifier import classify_error, classify_empty_result, summarize_response
from .dependencies import DependencyHealth
from .llm import LLMClient, LLMError
from .prompts import GRADE_ANALYSIS_SYSTEM, grade_analysis_user
from .render import (assemble, extract_session, format_grades, format_schedule,
                     preprocess_grades, strip_login_note)
from .runtime_metrics import RuntimeMetrics
from .schema import WorkflowRequest
from .trace import LOG, new_run_id


class ServiceError(RuntimeError):
    """上游教务服务返回的业务错误。"""

    def __init__(self, status_code: int, body: Any, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


_SESSION_INVALID_RE = re.compile(
    r"session.{0,32}(invalid|expired|失效|过期)|会话无效|会话已过期|"
    r"学校端会话|TokenError|重新登录",
    re.IGNORECASE,
)


class ServiceClient(Protocol):
    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...


class HTTPServiceClient:
    """httpx 实现：调用 get-infomation-service，附加 Bearer 令牌。"""

    def __init__(self, base_url: str, api_token: str = "", timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(self, path: str) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = "Bearer %s" % self.api_token
        try:
            resp = await self.client.get(self.base_url + path, headers=headers)
        except httpx.HTTPError as exc:
            raise ServiceError(0, None, "上游服务连接失败：%s" % exc.__class__.__name__) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise ServiceError(resp.status_code, data, "上游服务 HTTP %s" % resp.status_code)
        return data if isinstance(data, dict) else {"body": data}

    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = "Bearer %s" % self.api_token
        try:
            resp = await self.client.post(self.base_url + path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ServiceError(0, None, "上游服务连接失败：%s" % exc.__class__.__name__) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise ServiceError(resp.status_code, data, "上游服务 HTTP %s" % resp.status_code)
        return data if isinstance(data, dict) else {"body": data}


def _analysis_usage(usage: object) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "totalTokens", "total"):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value.is_integer() and value >= 0:
            return int(value)
    return None


class Pipeline:
    """一次查询的编排执行器；clients 可注入以便测试。"""

    def __init__(
        self,
        service: Optional[ServiceClient] = None,
        llm: Optional[LLMClient] = None,
        base_url: str = "https://school.lizf.cn",
        service_token: str = "",
        request_timeout: float = 100.0,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        pdf_semaphore: Optional[asyncio.Semaphore] = None,
        progress_cb: Optional[Callable[[str], Awaitable[None]]] = None,
        dependency_health: Optional[DependencyHealth] = None,
        metrics: Optional[RuntimeMetrics] = None,
    ) -> None:
        self.service = service or HTTPServiceClient(base_url, service_token)
        self.llm = llm or LLMClient()
        self.request_timeout = request_timeout
        self.llm_semaphore = llm_semaphore or asyncio.Semaphore(8)
        self.pdf_semaphore = pdf_semaphore or asyncio.Semaphore(2)
        self.progress_cb = progress_cb
        self.dependency_health = dependency_health
        self.metrics = metrics

    async def aclose(self) -> None:
        for client in (self.service, self.llm):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    async def _get_jump_body(
        self,
        session_info: Dict[str, str],
        run_id: str = "",
    ) -> Dict[str, Any]:
        """免密链接是输出增强，不是查询依赖；失败只降级，不阻断数据。"""
        try:
            # json=1：显式取结构化响应（login_url/app_url），避免依赖纯文本返回
            return await self._post("/jump", {
                "session": session_info["session"], "json": "1"})
        except ServiceError as exc:
            LOG.warning("run=%s 获取免密链接失败，降级处理", run_id)
            self._record_dependency(
                "query_proxy", "degraded",
                "免密链接获取失败，查询继续但返回内容不含跳转链接")
            return {}

    async def run(
        self,
        req: WorkflowRequest,
        session: Optional[str] = None,
        progress_cb: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        started = time.time()
        run_id = new_run_id()

        if session:
            session_info = {"session": session}
        else:
            # 1) 登录 → 2) 状态判断（body 是否含 session）
            try:
                login_body = await self._post("/login", {
                    "username": req.username, "password": req.password})
            except ServiceError as exc:
                LOG.warning("run=%s 登录请求失败", run_id)
                self._record_service_error(exc)
                result = classify_error("login", exc.status_code, exc.body,
                                        error_message=str(exc))
                result.update({"run_id": run_id,
                               "meta": {**result.get("meta", {}), "elapsed_ms": _elapsed(started)}})
                return result
            session_info = extract_session(login_body)
            if not session_info["session"]:
                LOG.warning("run=%s 登录失败", run_id)
                result = classify_error("login", body=login_body)
                result.update({"run_id": run_id, "meta": {**result.get("meta", {}),
                                                          "elapsed_ms": _elapsed(started)}})
                return result

        # 4) 查询项目分支；免密链接在数据查询成功后获取
        if req.option == "成绩":
            result = await self._run_grades(req, session_info, progress_cb)
        elif req.option == "课表":
            result = await self._run_schedule(req, session_info, progress_cb)
        else:
            result = classify_error("request", body="option=%s 非法" % req.option)

        result["run_id"] = run_id
        result["meta"] = {**result.get("meta", {}), "elapsed_ms": _elapsed(started)}
        return result

    async def _run_grades(
        self,
        req: WorkflowRequest,
        session_info: Dict[str, str],
        progress_cb: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        await self._report_phase("querying", progress_cb)
        payload = {
            "session": session_info["session"],
            "semesters": req.semesters or "",
            "include_rows": "false",
            "include_all": "false",
        }
        try:
            data, retried = await self._post_with_session_recovery(
                req, session_info, path="/get_grades", payload=payload)
        except ServiceError as exc:
            self._record_service_error(exc)
            return classify_error("grades", exc.status_code, exc.body,
                                  error_message=str(exc))
        if not _is_success(data):
            return classify_error("grades", body=data)
        jump_body = await self._get_jump_body(session_info)
        if classify_empty_result("grades", data):
            # success:true 且 count:0 → 正常无数据，不算故障
            output = format_grades(data, jump_body)
            return _ok("grades_empty", output,
                       meta={"count": 0, "response_summary": summarize_response(data)})

        if req.check:
            # 分析功能判断 true：预处理 → LLM 分析 → 数据组装
            await self._report_phase("analyzing", progress_cb)
            parts = preprocess_grades(data, jump_body)
            llm_wait_started = time.perf_counter()
            try:
                async with self.llm_semaphore:
                    if self.metrics is not None:
                        self.metrics.observe_concurrency_wait(
                            "llm", time.perf_counter() - llm_wait_started)
                    analysis, analysis_usage = await self.llm.chat_with_usage(
                        GRADE_ANALYSIS_SYSTEM,
                        grade_analysis_user(parts["table_text"], parts["stats_text"]))
                self._record_dependency(
                    "llm", "ok", "成绩分析 LLM 最近一次调用成功")
                if self.metrics is not None:
                    self.metrics.observe_llm(True)
            except LLMError as exc:
                LOG.warning("成绩分析 LLM 失败：%s，降级为纯成绩表", exc)
                analysis = ""
                analysis_usage = None
                self._record_dependency(
                    "llm", "degraded", "成绩分析 LLM 不可用，已降级为纯成绩表")
                if self.metrics is not None:
                    self.metrics.observe_llm(False)
            output = assemble(parts["prefix_text"], analysis)
        else:
            output = format_grades(data, jump_body)

        result = _ok("grades", output, meta={
            "count": data.get("count", 0),
            "response_summary": summarize_response(data),
        })
        if retried:
            result["meta"]["relogin_retry"] = True
        if req.check:
            result["meta"]["analysis_used"] = bool(analysis)
            usage_total = _analysis_usage(analysis_usage)
            if usage_total is not None:
                result["meta"]["analysis_usage"] = usage_total
        if req.md2pdf:
            await self._report_phase("generating_pdf", progress_cb)
            pdf_wait_started = time.perf_counter()
            async with self.pdf_semaphore:
                if self.metrics is not None:
                    self.metrics.observe_concurrency_wait(
                        "pdf", time.perf_counter() - pdf_wait_started)
                result["pdf_base64"] = await self._to_pdf_base64(strip_login_note(output))
            result["kind"] = "grades_pdf"
        return result

    async def _run_schedule(
        self,
        req: WorkflowRequest,
        session_info: Dict[str, str],
        progress_cb: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        await self._report_phase("querying", progress_cb)
        payload = {
            "session": session_info["session"],
            "weeks": req.weeks,
            "semester": req.semesters or "",
            "include_rows": "false",
        }
        try:
            data, retried = await self._post_with_session_recovery(
                req, session_info, path="/get_schedule", payload=payload)
        except ServiceError as exc:
            self._record_service_error(exc)
            return classify_error("schedule", exc.status_code, exc.body,
                                  error_message=str(exc))
        if not _is_success(data):
            return classify_error("schedule", body=data)
        jump_body = await self._get_jump_body(session_info)
        result = _ok("schedule", format_schedule(data, jump_body),
                     meta={"response_summary": summarize_response(data)})
        if retried:
            result["meta"]["relogin_retry"] = True
        return result

    async def _post_with_session_recovery(
        self,
        req: WorkflowRequest,
        session_info: Dict[str, str],
        path: str,
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool]:
        """首次查询的预热竞态可能表现为 401；这里只允许一次受控重登。"""
        retried = False
        try:
            data = await self._post(path, payload)
            return data, retried
        except ServiceError as exc:
            if not self._can_relogin_after_session_error(req, exc):
                raise

        retried = True
        login_payload = {"username": req.username, "password": req.password}
        try:
            login_body = await self._post("/login", login_payload)
            new_session_info = extract_session(login_body)
            if not new_session_info.get("session"):
                raise ServiceError(200, login_body, "重新登录未返回 session")
        except ServiceError as exc:
            self._record_service_error(exc)
            raise

        session_info["session"] = new_session_info["session"]
        retry_payload = {**payload, "session": session_info["session"]}
        data = await self._post(path, retry_payload)
        return data, retried

    @staticmethod
    def _can_relogin_after_session_error(
        req: WorkflowRequest,
        exc: ServiceError,
    ) -> bool:
        if exc.status_code != 401 or not req.password:
            return False
        body = exc.body if isinstance(exc.body, dict) else {"error": str(exc.body or "")}
        message = " ".join(str(part) for part in (
            body.get("error") or body.get("message") or "",
            str(exc),
        ) if part)
        return bool(_SESSION_INVALID_RE.search(message))

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.service.post(path, payload)

    def _record_dependency(self, key: str, state: str, message: str) -> None:
        if self.dependency_health is not None:
            self.dependency_health.record(key, state, message)

    def _record_service_error(self, exc: ServiceError) -> None:
        if exc.status_code == 0:
            self._record_dependency(
                "query_proxy", "error", "查询代理不可达，查询无法继续")
        elif exc.status_code >= 500:
            self._record_dependency(
                "query_proxy", "degraded", "查询代理上游异常，已返回分类错误")

    async def _report_phase(
        self,
        phase: str,
        progress_cb: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        callback = progress_cb or self.progress_cb
        if callback is not None:
            await callback(phase)

    async def _to_pdf_base64(self, markdown: str) -> str:
        # 延迟导入：reportlab 只在真正需要 PDF 时加载
        from .pdf import markdown_to_pdf
        raw = await _run_in_thread(lambda: markdown_to_pdf(markdown))
        return base64.b64encode(raw).decode("ascii")


def _is_success(data: Dict[str, Any]) -> bool:
    """结构化判断：替代原工作流对 body 字符串的 contains 判断。"""
    if not isinstance(data, dict):
        return False
    return bool(data.get("success"))


def _ok(kind: str, output: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"success": True, "kind": kind, "output": output, "meta": meta or {}}


def _elapsed(started: float) -> int:
    return int((time.time() - started) * 1000)


async def _run_in_thread(fn):
    """阻塞调用放到线程池，避免卡住事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)

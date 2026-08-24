"""查询编排主流程：把固定的 Dify 工作流图代码化为顺序分支。

对应原图：login → 状态判断 → 获取session → /jump → 查询项目分支 → 成绩/课表
→ 状态判断 → 渲染/分析/格式化 → (md2pdf 分支) → 合并输出。
迁移时已修复原编排中的两处问题：
1. 不再把登录响应拼入输出（原变量聚合器泄漏 session 的风险）；
2. PDF 分支直接依据结构化 success 字段判断，不再依赖失效的字符串条件。
"""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, Optional, Protocol

import httpx

from .classifier import classify_error, classify_empty_result
from .llm import LLMClient, LLMError
from .prompts import GRADE_ANALYSIS_SYSTEM, grade_analysis_user
from .render import assemble, extract_session, format_grades, format_schedule, preprocess_grades
from .schema import WorkflowRequest
from .trace import LOG, new_run_id


class ServiceError(RuntimeError):
    """上游教务服务返回的业务错误。"""

    def __init__(self, status_code: int, body: Any, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ServiceClient(Protocol):
    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...


class HTTPServiceClient:
    """httpx 实现：调用 get-infomation-service，附加 Bearer 令牌。"""

    def __init__(self, base_url: str, api_token: str = "", timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = "Bearer %s" % self.api_token
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(self.base_url + path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise ServiceError(0, None, "上游服务连接失败：%s" % exc.__class__.__name__) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise ServiceError(resp.status_code, data, "上游服务 HTTP %s" % resp.status_code)
        return data if isinstance(data, dict) else {"body": data}


class Pipeline:
    """一次查询的编排执行器；clients 可注入以便测试。"""

    def __init__(
        self,
        service: Optional[ServiceClient] = None,
        llm: Optional[LLMClient] = None,
        base_url: str = "https://school.lizf.cn",
        service_token: str = "",
    ) -> None:
        self.service = service or HTTPServiceClient(base_url, service_token)
        self.llm = llm or LLMClient()

    async def run(self, req: WorkflowRequest) -> Dict[str, Any]:
        started = time.time()
        run_id = new_run_id()

        # 1) 登录 → 2) 状态判断（body 是否含 session）
        login_body = await self._post("/login", {
            "username": req.username, "password": req.password})
        session_info = extract_session(login_body)
        if not session_info["session"]:
            LOG.warning("run=%s 登录失败", run_id)
            result = classify_error("login", body=login_body)
            result.update({"run_id": run_id, "meta": {**result.get("meta", {}),
                                                      "elapsed_ms": _elapsed(started)}})
            return result

        # 3) 获取免密登录链接（失败降级为无链接，不阻断查询）
        jump_body: Dict[str, Any] = {}
        try:
            jump_body = await self._post("/jump", {"session": session_info["session"]})
        except ServiceError:
            LOG.warning("run=%s 获取免密链接失败，降级处理", run_id)

        # 4) 查询项目分支
        if req.option == "成绩":
            result = await self._run_grades(req, session_info, jump_body)
        elif req.option == "课表":
            result = await self._run_schedule(req, session_info, jump_body)
        else:
            result = classify_error("request", body="option=%s 非法" % req.option)

        result["run_id"] = run_id
        result["meta"] = {**result.get("meta", {}), "elapsed_ms": _elapsed(started)}
        return result

    async def _run_grades(self, req: WorkflowRequest, session_info: Dict[str, str],
                          jump_body: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "session": session_info["session"],
            "semesters": req.semesters or "",
            "include_rows": "false",
            "include_all": "false",
        }
        try:
            data = await self._post("/get_grades", payload)
        except ServiceError as exc:
            return classify_error("grades", exc.status_code, exc.body,
                                  error_message=str(exc))

        if not _is_success(data):
            return classify_error("grades", body=data)
        if classify_empty_result("grades", data):
            # success:true 且 count:0 → 正常无数据，不算故障
            output = format_grades(data, jump_body)
            return _ok("grades_empty", output, meta={"count": 0})

        if req.check:
            # 分析功能判断 true：预处理 → LLM 分析 → 数据组装
            parts = preprocess_grades(data, jump_body)
            try:
                analysis = await self.llm.chat(
                    GRADE_ANALYSIS_SYSTEM,
                    grade_analysis_user(parts["table_text"], parts["stats_text"]))
            except LLMError as exc:
                LOG.warning("成绩分析 LLM 失败：%s，降级为纯成绩表", exc)
                analysis = ""
            output = assemble(parts["prefix_text"], analysis)
        else:
            output = format_grades(data, jump_body)

        result = _ok("grades", output, meta={"count": data.get("count", 0)})
        if req.md2pdf:
            result["pdf_base64"] = await self._to_pdf_base64(output)
            result["kind"] = "grades_pdf"
        return result

    async def _run_schedule(self, req: WorkflowRequest, session_info: Dict[str, str],
                            jump_body: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "session": session_info["session"],
            "weeks": req.weeks,
            "semester": req.semesters or "",
            "include_rows": "false",
        }
        try:
            data = await self._post("/get_schedule", payload)
        except ServiceError as exc:
            return classify_error("schedule", exc.status_code, exc.body,
                                  error_message=str(exc))

        if not _is_success(data):
            return classify_error("schedule", body=data)
        return _ok("schedule", format_schedule(data, jump_body))

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.service.post(path, payload)

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
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)

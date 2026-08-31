"""编排主流程测试：注入假客户端，验证分支逻辑。"""
from __future__ import annotations

from app.llm import LLMClient
from app.pipeline import Pipeline, ServiceError
from app.schema import SessionWorkflowRequest, WorkflowRequest


class FakeService:
    """记录调用路径并按规则返回。"""

    def __init__(self, grade_success: bool = True) -> None:
        self.grade_success = grade_success
        self.calls = []

    async def post(self, path, payload):
        self.calls.append(path)
        if path == "/login":
            return {"success": True, "session": "s1", "token": "t1",
                    "username": payload["username"]}
        if path == "/jump":
            return {"success": True, "url": "https://example.com/home",
                    "login_url": "https://example.com/jump/go?code=x"}
        if path == "/get_grades":
            if self.grade_success:
                return {"success": True, "count": 1,
                        "output": "| 学期 | 课程 | 成绩 |\n|---|---|---|\n| 1 | 高数 | 90 |"}
            return {"success": False, "error": "login verify failed"}
        if path == "/get_schedule":
            return {"success": True, "output": "| 节次 | 周一 |", "download_url": "https://x/dl"}
        raise AssertionError(path)


class FakeLLM:
    last_usage = {"total_tokens": 46}

    def __init__(self) -> None:
        self.called = False

    async def chat(self, system, user):
        self.called = True
        return "分析正文"


def _req(**kwargs):
    base = {"username": "2023000001", "password": "pw", "option": "成绩"}
    base.update(kwargs)
    return WorkflowRequest(**base)


async def test_grades_without_check():
    svc = FakeService()
    result = await Pipeline(service=svc, llm=FakeLLM()).run(_req())
    assert result["success"] and result["kind"] == "grades"
    assert "免密登录教务系统" in result["output"]
    assert "高数" in result["output"]
    assert "session" not in result["output"]  # 不泄漏登录响应
    assert "count\":1" in result["meta"]["response_summary"]


async def test_grades_with_check_calls_llm_and_assembles():
    svc = FakeService()
    llm = FakeLLM()
    result = await Pipeline(service=svc, llm=llm).run(_req(check=True))
    assert llm.called
    assert result["output"].endswith("分析正文")
    assert result["meta"]["analysis_used"] is True
    assert result["meta"]["analysis_usage"] == 46
    assert "count\":1" in result["meta"]["response_summary"]
    assert "TABLE".lower() in result["output"].lower() or "高数" in result["output"]


async def test_login_connection_failure_is_classified_not_500():
    class DownService(FakeService):
        async def post(self, path, payload):
            if path == "/login":
                raise ServiceError(0, None, "上游服务连接失败：ConnectTimeout")
            return await super().post(path, payload)

    result = await Pipeline(service=DownService(), llm=FakeLLM()).run(_req())
    assert not result["success"]
    assert result["kind"] == "login_error"
    assert "上游服务连接失败" in result["output"]
    assert "ConnectTimeout" in result["meta"]["response_summary"]


async def test_login_failure_returns_friendly_report():
    class BadLogin(FakeService):
        async def post(self, path, payload):
            if path == "/login":
                return {"success": False, "error": "账号或密码错误"}
            return await super().post(path, payload)

    result = await Pipeline(service=BadLogin(), llm=FakeLLM()).run(_req())
    assert not result["success"]
    assert result["kind"] == "login_error"
    assert "账号或密码错误" in result["output"]


async def test_schedule_branch():
    svc = FakeService()
    result = await Pipeline(service=svc, llm=FakeLLM()).run(_req(option="课表"))
    assert result["success"] and result["kind"] == "schedule"
    assert result["output"].startswith("[点击下载课表（PDF 文件）]")
    assert "download_url" in result["meta"]["response_summary"]


async def test_session_job_skips_password_login():
    svc = FakeService()
    req = SessionWorkflowRequest(
        username="2023000001", option="成绩", password=None)
    result = await Pipeline(service=svc, llm=FakeLLM()).run(req, session="ticket-1")
    assert result["success"]
    assert svc.calls == ["/jump", "/get_grades"]


async def test_session_grade_job_reports_phase_transitions():
    svc = FakeService()
    phases = []

    async def report_phase(phase):
        phases.append(phase)

    req = SessionWorkflowRequest(
        username="2023000001", option="成绩", password=None, check=True)
    result = await Pipeline(service=svc, llm=FakeLLM()).run(
        req, session="ticket-1", progress_cb=report_phase)

    assert result["success"]
    assert phases == ["querying", "analyzing"]


async def test_session_schedule_job_reports_query_phase():
    svc = FakeService()
    phases = []

    async def report_phase(phase):
        phases.append(phase)

    req = SessionWorkflowRequest(
        username="2023000001", option="课表", password=None)
    result = await Pipeline(service=svc, llm=FakeLLM()).run(
        req, session="ticket-1", progress_cb=report_phase)

    assert result["success"]
    assert phases == ["querying"]


async def test_llm_and_pdf_semaphores_are_accepted():
    import asyncio
    pipeline = Pipeline(
        service=FakeService(), llm=FakeLLM(),
        llm_semaphore=asyncio.Semaphore(1),
        pdf_semaphore=asyncio.Semaphore(1))
    assert pipeline.llm_semaphore._value == 1
    assert pipeline.pdf_semaphore._value == 1

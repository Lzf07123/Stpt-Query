"""编排主流程测试：注入假客户端，验证分支逻辑。"""
from __future__ import annotations

from app.llm import LLMClient
from app.pipeline import Pipeline, ServiceError
from app.schema import WorkflowRequest


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
            return {"url": "https://example.com/ticket?st=x"}
        if path == "/get_grades":
            if self.grade_success:
                return {"success": True, "count": 1,
                        "output": "| 学期 | 课程 | 成绩 |\n|---|---|---|\n| 1 | 高数 | 90 |"}
            return {"success": False, "error": "login verify failed"}
        if path == "/get_schedule":
            return {"success": True, "output": "| 节次 | 周一 |", "download_url": "https://x/dl"}
        raise AssertionError(path)


class FakeLLM:
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


async def test_grades_with_check_calls_llm_and_assembles():
    svc = FakeService()
    llm = FakeLLM()
    result = await Pipeline(service=svc, llm=llm).run(_req(check=True))
    assert llm.called
    assert result["output"].endswith("分析正文")
    assert "TABLE".lower() in result["output"].lower() or "高数" in result["output"]


async def test_login_failure_returns_friendly_report():
    class BadLogin(FakeService):
        async def post(self, path, payload):
            if path == "/login":
                return {"success": False, "error": "账号或密码错误"}
            return await super().post(path, payload)

    result = await Pipeline(service=BadLogin(), llm=FakeLLM()).run(_req())
    assert not result["success"]
    assert result["kind"] == "login_error"
    assert "凭据被拒绝" in result["output"]


async def test_schedule_branch():
    svc = FakeService()
    result = await Pipeline(service=svc, llm=FakeLLM()).run(_req(option="课表"))
    assert result["success"] and result["kind"] == "schedule"
    assert result["output"].startswith("[点击下载课表（Word 文件）]")

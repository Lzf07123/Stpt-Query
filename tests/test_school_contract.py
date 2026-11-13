"""学校侧契约基础设施测试：单一来源强制、注册表黄金路径、分层探测、健康契约。

- 反重复扫描：学校侧字面量只允许出现在 school_contract.py（「升级改一处」的强制保证）；
- 黄金路径：注册表渲染结果与历史上游 URL 逐字节一致（零行为变化的核心证据）；
- 分层探测 / deep_check / health 契约：沿用既有 Fake/monkeypatch 隔离模式，不触网。
"""

import asyncio
import contextlib
import re
import types
from pathlib import Path

import pytest

from app import jwxt_state
from app import school_contract as contract
from app import school_endpoints as ep
from app.classifier import classify_error

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# 学校侧字面量两级黑名单：
# - 主机/端口/盐/协议材料：只允许 school_contract.py（改一处即恢复服务的核心保证）
# - 路径模板类：允许契约模块 + school_endpoints.py（注册表本身就是路径事实的家）
FORBIDDEN_ANYWHERE = (
    "stpt", "10.1.200.", "lyedu", "lyasp", "manager-login",
    "CASTGC", "XSFWDZ",
)
FORBIDDEN_OUTSIDE_CONTRACT = FORBIDDEN_ANYWHERE + (
    "lyuap", "vpn-12", "rump_frontend", "getLoginParam", "redirectToCas",
    "loginFromCas", "getHomeParam", "getAppList", "/webvpn/",
)
PATH_FACT_ALLOWED = {"school_contract.py", "school_endpoints.py"}

VPN_EDGE = contract.VPN_BASE + "/"
PORTAL_PARAM_URL = contract.VPN_BASE + contract.PORTAL_LOGIN_PARAM
PORTAL_REDIRECT_URL = contract.VPN_BASE + contract.PORTAL_REDIRECT_TO_CAS


# ---------------------------------------------------------------------------
# 单一来源强制
# ---------------------------------------------------------------------------

def _scan_forbidden(allowed):
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lit in FORBIDDEN_OUTSIDE_CONTRACT:
            if lit in text:
                offenders.append("%s: %r" % (rel, lit))
    return offenders


def test_school_literals_live_only_in_contract():
    offenders = _scan_forbidden(PATH_FACT_ALLOWED)
    assert offenders == [], ("学校侧事实必须只存在于契约/注册表模块（见 "
                             "docs/upstream-contract.md）： " + "; ".join(offenders))


def test_host_and_protocol_facts_live_only_in_contract():
    """主机/端口/盐/协议材料比路径更敏感：连注册表模块也不得复制。"""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel == "school_contract.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lit in FORBIDDEN_ANYWHERE:
            if lit in text:
                offenders.append("%s: %r" % (rel, lit))
    assert offenders == [], ("主机/盐/协议事实必须只存在于 school_contract.py： "
                             + "; ".join(offenders))


def test_contract_reexports_stable():
    """旧名兼容：jwxt_state/jwxt_http/tests 依赖的 jwxt_core 旧名保持可用。"""
    from app import jwxt_core as core
    assert core.BASE == contract.VPN_BASE
    assert core.SERVICE == contract.PORTAL_SERVICE
    assert core.RUMP_HOST == contract.VPN_HOST_WITH_PORT
    assert core.MGMT_SSO_SERVICE == contract.MGMT_SSO_SERVICE
    assert core.SESSION_INVALID_CODES is contract.SESSION_INVALID_CODES
    assert core.SESSION_INVALID_HINTS is contract.SESSION_INVALID_HINTS


# ---------------------------------------------------------------------------
# 注册表黄金路径：渲染结果与历史上游 URL 逐字节一致
# ---------------------------------------------------------------------------

GOLDEN_PATHS = {
    "portal.login_param": "/rump_frontend/getLoginParam/",
    "portal.redirect_to_cas": "/rump_frontend/redirectToCas/",
    "portal.login_from_cas": "/rump_frontend/loginFromCas/?ticket=ST-9",
    "portal.home_param": "/rump_frontend/getHomeParam/",
    "portal.app_list": "/rump_frontend/getAppList/",
    "portal.login_page": "/rump_frontend/login/",
    "webvpn.root": "/",
    "webvpn.cookie_write": "/webvpn/cookie/",
    "webvpn.cookie_read": "/webvpn/cookie/",
    "cas.login_page": "/lyuapServer/login",
    "cas.tickets": "/lyuapServer/v1/tickets?vpn-12-cas.stpt.edu.cn",
    "cas.tickets_redeem": "/lyuapServer/v1/tickets/TGT-abc?vpn-12-cas.stpt.edu.cn",
    "mgmt.cas_login": "api/cas/login?pattern=manager-login&ticket=ST-123",
    "mgmt.cas_login_probe": "api/cas/login?pattern=manager-login&returnUrl=http%3A%2F%2Fx",
    "mgmt.sysset_param":
        "api/system/sysSet/selectByParamCode?vpn-12-10.1.200.4:9090&paramCode=XSFWDZ&_t=%d",
    "student.current_semester":
        "api/baseInfo/semester/selectCurrentXnXq?vpn-12-10.1.200.4:9995&_t=%d",
    "student.semester_list":
        "api/baseInfo/semester/selectXnXqListTy?vpn-12-10.1.200.4:9995&_t=%d",
    "student.finals":
        "api/score/scorequerymanage/studentFinalQuery?vpn-12-10.1.200.4:9995&_t=%d",
    "student.info":
        "api/student/studentInfo/selectXsSyByid/2023000001?vpn-12-10.1.200.4:9995&_t=%d",
    "student.university_score":
        "api/score/universityScore/queryUniversityScore?vpn-12-10.1.200.4:9995&_t=%d",
    "student.credit_situation":
        "api/score/universityScore/queryCreditSituation?vpn-12-10.1.200.4:9995&_t=%d",
    "student.course_schedule":
        "api/arrange/CourseScheduleAllQuery/studentCourseSchedule?vpn-12-10.1.200.4:9995&_t=%d",
    "student.schedule_export":
        "api/arrange/courseSchedulePrint/export?courseType=studentSchedule&vpn-12-10.1.200.4:9995&_t=%d",
}

GOLDEN_PARAMS = {
    "portal.login_from_cas": {"ticket": "ST-9"},
    "cas.tickets_redeem": {"tgt": "TGT-abc"},
    "mgmt.cas_login": {"pattern": "manager-login", "ticket": "ST-123"},
    "mgmt.cas_login_probe": {"pattern": "manager-login", "return_url": "http%3A%2F%2Fx"},
    "student.info": {"xh": "2023000001"},
    "student.schedule_export": {"course_type": "studentSchedule"},
}


def test_registry_renders_historical_urls_byte_for_byte():
    for name, want in GOLDEN_PATHS.items():
        got = ep.render(name, **GOLDEN_PARAMS.get(name, {}))
        assert got == want, "%s 渲染结果与历史 URL 不一致：%r != %r" % (name, got, want)


def test_registry_names_unique_and_layers_valid():
    names = ep.endpoint_names()
    assert len(names) == len(set(names))
    valid_layers = {contract.LAYER_WEBVPN, contract.LAYER_CAS, contract.LAYER_PORTAL,
                    contract.LAYER_MGMT, contract.LAYER_STUDENT}
    valid_bases = {ep.BASE_VPN, ep.BASE_CAS, ep.BASE_MGMT, ep.BASE_STUDENT}
    for name in names:
        spec = ep.get(name)
        assert spec.layer in valid_layers
        assert spec.base in valid_bases
    with pytest.raises(KeyError):
        ep.get("student.not_registered")


def test_api_path_composition_order_is_historical():
    """查询串组合顺序与历史 URL 一致：leading → vpn 主机映射 → trailing → _t。"""
    assert contract.api_path("api/x") == "api/x"
    assert contract.api_path("api/x", vpn_host="h:1", with_ts=False) == "api/x?vpn-12-h:1"
    assert contract.api_path("api/x", vpn_host="h:1", trailing="b=2") == "api/x?vpn-12-h:1&b=2&_t=%d"
    assert contract.api_path("api/x", leading="a=1", vpn_host="h:1") == "api/x?a=1&vpn-12-h:1&_t=%d"


# ---------------------------------------------------------------------------
# probe_layers：无凭据分层探测（Fake Session，不触网）
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """按谓词脚本化响应的假会话（probe_layers 只用 get/mount/close）。"""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.headers = {}

    def mount(self, *a, **kw):
        pass

    def close(self):
        pass

    def get(self, url, timeout=None, allow_redirects=True, headers=None, verify=None):
        self.calls.append(url)
        for match, resp in self.script:
            if match(url):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError("probe 未预期的 URL: %s" % url)


def _patch_probe(monkeypatch, script):
    monkeypatch.setattr(jwxt_state, "requests",
                        types.SimpleNamespace(Session=lambda: _FakeSession(script)))
    monkeypatch.setattr(jwxt_state, "_upstream_slot", lambda: contextlib.nullcontext())


CAS_LINK_HTML = ('<a href="https://sec.stpt.edu.cn/webvpn/xx/https/cas.example/lyuapServer/login'
                 '?service=abc">登录</a>')
BUNDLE_HTML = '<script src="/app.9f3a.js"></script>'
BUNDLE_JS = 'return {public_exponent:"10001", modulus:"a1b2c3d4e5f60718"}'


def _happy_script():
    return [
        (lambda u: u == VPN_EDGE, _FakeResp(200, "edge")),
        (lambda u: u == PORTAL_PARAM_URL,
         _FakeResp(200, payload={"code": 0, "data": {"auth": {"master": "cas"}}})),
        (lambda u: u == PORTAL_REDIRECT_URL, _FakeResp(200, CAS_LINK_HTML)),
        (lambda u: "lyuapServer/login" in u, _FakeResp(200, BUNDLE_HTML)),
        (lambda u: "app.9f3a.js" in u, _FakeResp(200, BUNDLE_JS)),
    ]


def test_probe_layers_all_ok_when_contract_holds(monkeypatch):
    _patch_probe(monkeypatch, _happy_script())
    report = jwxt_state.probe_layers()
    assert report["layers"]["webvpn"]["ok"] is True
    assert report["layers"]["portal"]["ok"] is True
    assert report["layers"]["cas"]["ok"] is True
    names = {c["name"] for c in report["checks"]}
    assert {"webvpn.edge_reachable", "portal.auth_master", "portal.cas_link",
            "cas.login_page", "cas.bundle_js", "cas.rsa_exponent",
            "cas.rsa_modulus"} <= names
    assert all(c["ok"] for c in report["checks"])


def test_probe_layers_detects_portal_drift_and_continues(monkeypatch):
    """门户结构漂移（auth.master 改变、无 CAS 链接）时：portal 层 False、
    CAS 层未判定（缺地址），且其余探测不中断。"""
    script = [
        (lambda u: u == VPN_EDGE, _FakeResp(200, "edge")),
        (lambda u: u == PORTAL_PARAM_URL,
         _FakeResp(200, payload={"code": 0, "data": {"auth": {"master": "oauth2"}}})),
        (lambda u: u == PORTAL_REDIRECT_URL, _FakeResp(200, "<html>no link</html>")),
    ]
    _patch_probe(monkeypatch, script)
    report = jwxt_state.probe_layers()
    assert report["layers"]["portal"]["ok"] is False
    assert report["layers"]["cas"]["ok"] is None
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["portal.auth_master"]["ok"] is False
    assert "oauth2" in by_name["portal.auth_master"]["detail"]
    assert by_name["portal.cas_link"]["ok"] is False
    assert by_name["cas.login_page"]["ok"] is None


def test_probe_layers_webvpn_down_marks_all_unreachable(monkeypatch):
    _patch_probe(monkeypatch, [
        (lambda u: u == VPN_EDGE, ConnectionError("dns fail")),
    ])
    report = jwxt_state.probe_layers()
    assert report["layers"]["webvpn"]["ok"] is False
    assert report["layers"]["portal"]["ok"] is False
    # CAS 未获得地址 → 未判定而非误报
    assert report["layers"]["cas"]["ok"] is None


# ---------------------------------------------------------------------------
# deep_check：分层归因（Fake J，不触网）
# ---------------------------------------------------------------------------

class _FakeJ:
    def __init__(self, u, p, fail_at=None):
        self.u, self.p = u, p
        self.fail_at = fail_at
        self.closed = False

    def _fail(self, stage):
        if self.fail_at == stage:
            raise jwxt_state.SessionInvalidError("学校端会话已失效: xxx")

    def login(self):
        self._fail("login")

    def jwxt(self):
        self._fail("jwxt")

    def student(self):
        self._fail("student")

    def current(self):
        self._fail("current")
        return "2025-2026-1"

    def close(self):
        self.closed = True


def _patch_deep(monkeypatch, fail_at=None, probe=None):
    monkeypatch.setattr(jwxt_state, "J",
                        lambda u, p: _FakeJ(u, p, fail_at=fail_at))
    monkeypatch.setattr(jwxt_state, "probe_layers",
                        lambda timeout=8: probe or {"layers": {}, "checks": []})


_PROBE_OK = {"layers": {"webvpn": {"ok": True, "latency_ms": 1},
                        "cas": {"ok": True, "latency_ms": 1},
                        "portal": {"ok": True, "latency_ms": 1}},
             "checks": []}


def test_deep_check_success_layers_and_stages(monkeypatch):
    state = jwxt_state.ServerState()
    _patch_deep(monkeypatch, fail_at=None, probe=_PROBE_OK)
    result = jwxt_state.deep_check("u", "p", state=state)
    assert result["ok"] is True
    assert result["semester"] == "2025-2026-1"
    assert result["contract_version"] == contract.SCHOOL_CONTRACT_VERSION
    assert result["layers"]["jwxt"]["ok"] is True
    assert result["layers"]["webvpn"]["ok"] is True
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["login"]["ok"] is True
    assert stages["login"]["layer"] == contract.LAYER_PORTAL
    assert stages["jwxt"]["layer"] == contract.LAYER_MGMT
    assert stages["api_current_semester"]["layer"] == contract.LAYER_STUDENT
    assert state.deep["ok"] is True


def test_deep_check_login_failure_keeps_jwxt_unknown(monkeypatch):
    probe = {"layers": {"webvpn": {"ok": True, "latency_ms": 1},
                        "cas": {"ok": False, "latency_ms": 1, "error": "HTTP 503"},
                        "portal": {"ok": True, "latency_ms": 1}},
             "checks": []}
    _patch_deep(monkeypatch, fail_at="login", probe=probe)
    result = jwxt_state.deep_check("u", "p")
    assert result["ok"] is False
    assert result["layers"]["cas"]["ok"] is False
    # 教务层未测到 → 三态 None，不误报为 False
    assert result["layers"]["jwxt"]["ok"] is None
    stages = {s["stage"]: s for s in result["stages"] if s.get("layer")}
    assert stages["login"]["ok"] is False
    assert "SessionInvalidError" in stages["login"]["error"]


def test_deep_check_jwxt_failure_attributes_jwxt_layer(monkeypatch):
    _patch_deep(monkeypatch, fail_at="current", probe=_PROBE_OK)
    result = jwxt_state.deep_check("u", "p")
    assert result["ok"] is False
    assert result["layers"]["jwxt"]["ok"] is False
    assert result["layers"]["webvpn"]["ok"] is True


# ---------------------------------------------------------------------------
# /health 契约：layers 与 contract_version（additive）
# ---------------------------------------------------------------------------

def _bare_state(school_layers=None, deep=None):
    st = jwxt_state.ServerState()
    st.net = {"ok": True, "latency_ms": 5, "at": 1}
    st.school_layers = school_layers
    st.deep = deep
    return st


def test_health_payload_layers_and_contract_version():
    state = _bare_state(
        school_layers={"at": 9, "layers": {
            "webvpn": {"ok": True},
            "cas": {"ok": False, "error": "HTTP 503: bad gateway"}}},
        deep={"layers": {"jwxt": {"ok": None}}})
    payload = jwxt_state.health_payload(state, types.SimpleNamespace(health_credentials=None))
    assert payload["contract_version"] == contract.SCHOOL_CONTRACT_VERSION
    assert payload["school"]["layers"] == {
        "webvpn": {"ok": True},
        "cas": {"ok": False, "error": "HTTP 503: bad gateway"},
    }
    # jwxt 未判定（ok=None）不进公开层，避免把「未测」当成「故障」


def test_health_payload_omits_layers_without_source():
    state = _bare_state()
    payload = jwxt_state.health_payload(state, types.SimpleNamespace(health_credentials=None))
    assert "layers" not in payload["school"]
    assert payload["contract_version"] == contract.SCHOOL_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# /health/public 投影：分层三态 up/down/unknown，不暴露内部细节
# ---------------------------------------------------------------------------

class _StubResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _StubClient:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, headers=None, timeout=None):
        return _StubResp(self._payload)


class _StubRegistry:
    def __init__(self):
        self.records = []

    def record(self, key, state):
        self.records.append((key, state))


def _stub_ctx(payload):
    import app.context as context
    ctx = context.AppContext.__new__(context.AppContext)
    ctx.public_health_cache = {"payload": None, "at": 0.0}
    ctx.service_base_url = "http://jwxt.internal"
    ctx.service_api_token = "t"
    ctx.upstream_client = _StubClient(payload)
    ctx.app = types.SimpleNamespace(
        state=types.SimpleNamespace(dependency_health=_StubRegistry()))
    return ctx


def test_public_health_layers_projection():
    ctx = _stub_ctx({
        "status": "ok",
        "school": {"ok": True, "latency_ms": 5, "layers": {
            "webvpn": {"ok": True}, "cas": {"ok": False}, "portal": {"ok": None}}},
    })
    payload = asyncio.run(ctx.public_health_payload())
    assert payload["school"]["status"] == "up"
    assert payload["school"]["layers"] == {
        "webvpn": "up", "cas": "down", "portal": "unknown"}


def test_public_health_omits_layers_without_source():
    ctx = _stub_ctx({"status": "ok", "school": {"ok": True, "latency_ms": 5}})
    payload = asyncio.run(ctx.public_health_payload())
    assert payload["school"]["status"] == "up"
    assert "layers" not in payload["school"]


# ---------------------------------------------------------------------------
# classifier 分层归因（meta.layer，additive）
# ---------------------------------------------------------------------------

def test_classifier_layer_cas_for_credential_errors():
    result = classify_error("login", body="PASSERROR")
    assert result["meta"]["category"] == "账号或密码错误"
    assert result["meta"]["layer"] == "cas"


def test_classifier_layer_portal_for_session_invalid():
    result = classify_error("grades", error_type="SessionInvalidError")
    assert result["meta"]["category"] == "会话失效或已过期"
    assert result["meta"]["layer"] == "portal"


def test_classifier_layer_none_when_unattributable():
    result = classify_error("grades", error_message="完全无法归类的奇怪故障")
    assert result["meta"]["category"] == "未分类异常"
    assert result["meta"]["layer"] is None


# ---------------------------------------------------------------------------
# J.call：注册表通用调用器（Fake req，不触网）
# ---------------------------------------------------------------------------

def test_j_call_student_endpoint_builds_historical_url(monkeypatch):
    import app.jwxt_core as core
    from app.jwxt_core import J
    captured = {}

    def fake_req(self, method, url, **kw):
        captured.update({"method": method, "url": url, "kw": kw})
        return _FakeResp(payload={"code": 200, "data": {"semester": "2025-2026-1"}})

    monkeypatch.setattr(core.J, "req", fake_req)
    j = J("2023000001", "pw")
    j.stu = "https://portal.example/"
    out = j.call("student.current_semester")
    assert out == {"semester": "2025-2026-1"}
    assert captured["method"] == "GET"
    assert captured["url"].startswith(
        "https://portal.example/api/baseInfo/semester/selectCurrentXnXq?vpn-12-10.1.200.4:9995&_t=")
    headers = captured["kw"]["headers"]
    assert re.fullmatch(r"[0-9a-f]{32}", headers["csrfToken"])
    assert headers["Referer"] == "https://portal.example/"


def test_j_call_mgmt_endpoint_and_raw_mode(monkeypatch):
    import app.jwxt_core as core
    from app.jwxt_core import J

    def fake_req(self, method, url, **kw):
        assert url.startswith("https://mgmt.example/api/system/sysSet/selectByParamCode?")
        assert "paramCode=XSFWDZ" in url
        return _FakeResp(payload={"code": 200, "data": {"paraValue": "http://stu/"}})

    monkeypatch.setattr(core.J, "req", fake_req)
    j = J("2023000001", "pw")
    j.mgmt = "https://mgmt.example/"
    out = j.call("mgmt.sysset_param")
    assert out == {"paraValue": "http://stu/"}


def test_j_call_rejects_non_json_layers():
    from app.jwxt_core import J
    j = J("2023000001", "pw")
    with pytest.raises(ValueError):
        j.call("cas.tickets")
    with pytest.raises(KeyError):
        j.call("student.not_registered")

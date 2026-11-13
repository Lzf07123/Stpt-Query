"""上游接口注册表：已探明的学校侧 HTTP 端点清单与模板渲染。

- 这是复刻整个教务系统的「接口面挂载点」：新增考试安排/选课/评教等模块时，
  先在这里登记 EndpointSpec，再用 J.call()（jwxt_core）调用，无需改动认证编排。
- 每条 spec 的路径模板经 render() 组合后与学校侧历史 URL 逐字节一致；
  黄金字符串测试（tests/test_school_contract.py）锁定这一点。
- 层（layer）与基址（base）分开：layer 用于分层健康归因，base 指明 URL 前缀
  （vpn=WebVPN 入口、cas=CAS 根、mgmt=教务管理端、student=学生服务）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.school_contract import (
    LAYER_CAS,
    LAYER_MGMT,
    LAYER_PORTAL,
    LAYER_STUDENT,
    LAYER_WEBVPN,
    api_path,
    CAS_VPN_HOST,
    MGMT_SYSSET_PATH,
    MGMT_VPN_HOST,
    STUDENT_VPN_HOST,
    SYSSET_PARAM_CODE,
)

BASE_VPN = "vpn"        # 前缀 VPN_BASE（WebVPN 入口，门户/Cookie 桥）
BASE_CAS = "cas"        # 前缀会话内 CAS 根地址（J.cas）
BASE_MGMT = "mgmt"      # 前缀教务管理端地址（J.mgmt）
BASE_STUDENT = "student"  # 前缀学生服务地址（J.stu）


@dataclass(frozen=True)
class EndpointSpec:
    """单个上游端点的契约描述。

    path 支持 {param} 占位（如 {xh}、{tgt}、{ticket}），由 render() 按调用参数
    替换；with_ts=True 时路径模板以 &_t=%d 结尾，由调用方（J.api/J.call）在请求
    前用时间戳格式化。leading_query/trailing_query 相对 vpn-12 后缀的位置与
    学校侧历史 URL 保持一致（见 school_contract.api_path 的组合顺序）。
    """

    name: str
    layer: str
    base: str
    method: str
    path: str
    vpn_host: str = ""
    leading_query: str = ""
    trailing_query: str = ""
    with_ts: bool = False
    needs_csrf: bool = False   # 学生服务/管理端 JSON API 需 csrfToken 头
    description: str = ""


_ENDPOINTS = (
    # --- WebVPN 边缘 ---
    EndpointSpec("webvpn.root", LAYER_WEBVPN, BASE_VPN, "GET", "/",
                 description="WebVPN 入口首页（可达性探测/预热）"),
    EndpointSpec("webvpn.cookie_write", LAYER_WEBVPN, BASE_VPN, "POST",
                 "/webvpn/cookie/",
                 description="写入跨域虚拟 Cookie（CAS 会话桥）"),
    EndpointSpec("webvpn.cookie_read", LAYER_WEBVPN, BASE_VPN, "GET",
                 "/webvpn/cookie/", with_ts=False,
                 description="读回跨域虚拟 Cookie（TGT Cookie 兜底取回）"),
    # --- rump_frontend 门户 ---
    EndpointSpec("portal.login_param", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/getLoginParam/",
                 description="登录参数（auth.master 判定认证方式）"),
    EndpointSpec("portal.redirect_to_cas", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/redirectToCas/",
                 description="返回携带 CAS 登录链接的跳转页"),
    EndpointSpec("portal.login_from_cas", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/loginFromCas/",
                 trailing_query="ticket={ticket}",
                 description="凭 ticket 建立门户会话"),
    EndpointSpec("portal.home_param", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/getHomeParam/",
                 description="门户会话校验（比对学号）"),
    EndpointSpec("portal.app_list", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/getAppList/",
                 description="应用列表（发现教务管理端地址）"),
    EndpointSpec("portal.login_page", LAYER_PORTAL, BASE_VPN, "GET",
                 "/rump_frontend/login/",
                 description="门户登录页（全新设备预热直达）"),
    # --- CAS 统一身份认证 ---
    EndpointSpec("cas.login_page", LAYER_CAS, BASE_CAS, "GET",
                 "/lyuapServer/login",
                 description="CAS 登录页（RSA 公钥 JS 引用来源）"),
    EndpointSpec("cas.tickets", LAYER_CAS, BASE_CAS, "POST",
                 "/lyuapServer/v1/tickets", vpn_host=CAS_VPN_HOST,
                 description="凭据换票（返回 tgt + ticket）"),
    EndpointSpec("cas.tickets_redeem", LAYER_CAS, BASE_CAS, "POST",
                 "/lyuapServer/v1/tickets/{tgt}", vpn_host=CAS_VPN_HOST,
                 description="TGT 换 service ticket（免密跳转）"),
    # --- 教务系统管理端（:9090） ---
    EndpointSpec("mgmt.cas_login", LAYER_MGMT, BASE_MGMT, "GET",
                 "api/cas/login",
                 trailing_query="pattern={pattern}&ticket={ticket}",
                 description="管理端 SSO 落点（ticket 免密）"),
    EndpointSpec("mgmt.cas_login_probe", LAYER_MGMT, BASE_MGMT, "GET",
                 "api/cas/login",
                 trailing_query="pattern={pattern}&returnUrl={return_url}",
                 description="经 302 Location 探测管理端 CAS service"),
    EndpointSpec("mgmt.sysset_param", LAYER_MGMT, BASE_MGMT, "GET",
                 MGMT_SYSSET_PATH, vpn_host=MGMT_VPN_HOST,
                 trailing_query="paramCode=" + SYSSET_PARAM_CODE,
                 with_ts=True, needs_csrf=True,
                 description="读取系统参数（学生服务地址由该参数给出）"),
    # --- 教务系统学生服务（:9995） ---
    EndpointSpec("student.current_semester", LAYER_STUDENT, BASE_STUDENT,
                 "GET", "api/baseInfo/semester/selectCurrentXnXq",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="当前学年学期"),
    EndpointSpec("student.semester_list", LAYER_STUDENT, BASE_STUDENT,
                 "GET", "api/baseInfo/semester/selectXnXqListTy",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="全部学期列表"),
    EndpointSpec("student.finals", LAYER_STUDENT, BASE_STUDENT,
                 "POST", "api/score/scorequerymanage/studentFinalQuery",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="学期成绩分页查询"),
    EndpointSpec("student.info", LAYER_STUDENT, BASE_STUDENT,
                 "GET", "api/student/studentInfo/selectXsSyByid/{xh}",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="学籍信息（按学号）"),
    EndpointSpec("student.university_score", LAYER_STUDENT, BASE_STUDENT,
                 "POST", "api/score/universityScore/queryUniversityScore",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="个人首页全部成绩分页查询"),
    EndpointSpec("student.credit_situation", LAYER_STUDENT, BASE_STUDENT,
                 "POST", "api/score/universityScore/queryCreditSituation",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="学分概况（总学分/已修/进度）"),
    EndpointSpec("student.course_schedule", LAYER_STUDENT, BASE_STUDENT,
                 "POST", "api/arrange/CourseScheduleAllQuery/studentCourseSchedule",
                 vpn_host=STUDENT_VPN_HOST, with_ts=True, needs_csrf=True,
                 description="我的课表（按学期/周次/单双周）"),
    EndpointSpec("student.schedule_export", LAYER_STUDENT, BASE_STUDENT,
                 "POST", "api/arrange/courseSchedulePrint/export",
                 vpn_host=STUDENT_VPN_HOST,
                 leading_query="courseType={course_type}",
                 with_ts=True, needs_csrf=True,
                 description="课表导出（RTF，courseType=studentSchedule）"),
)

_REGISTRY = {spec.name: spec for spec in _ENDPOINTS}


def get(name: str) -> EndpointSpec:
    """按名称取端点契约；未注册的名称直接报错（注册表是唯一挂载点）。"""
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError("未注册的上游端点：%s（请在 school_endpoints 登记）" % name)
    return spec


def render(name: str, **params) -> str:
    """渲染端点路径模板：拼接查询串并替换 {param} 占位。

    - 查询串组合顺序统一走 school_contract.api_path（单一组合逻辑，
      与学校侧历史 URL 逐字节一致，黄金字符串测试锁定）；
    - with_ts=True 的模板以 &_t=%d 结尾，时间戳由调用方格式化（J.api 现有约定）；
    - 占位替换使用 str.replace（不经 % 格式化），参数值含 % 等字符也安全。
    """
    spec = get(name)
    url = api_path(spec.path, vpn_host=spec.vpn_host,
                   leading=spec.leading_query, trailing=spec.trailing_query,
                   with_ts=spec.with_ts)
    for key, value in params.items():
        url = url.replace("{%s}" % key, str(value))
    return url


def endpoint_names():
    """全部已注册端点名（自检/文档工具用）。"""
    return tuple(_REGISTRY.keys())

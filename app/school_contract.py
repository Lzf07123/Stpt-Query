"""学校侧契约：上游四层链路全部「学校侧事实」的单一事实来源。

本项目通过以下四层链路对接学校教务系统（全部事实经实测取证，无公开文档）：

    WebVPN 边缘（URL 加密 + Cookie 桥）
      └─ CAS 统一身份认证（RSA 加密票据）
           └─ rump_frontend 门户前端
                └─ 教务系统（管理端 :9090 + 学生服务 :9995）

学校侧升级（换域名、换认证产品、改接口路径、轮换 RSA 密钥、改会话失效文案）
时，按 docs/upstream-contract.md 的应急流程更新本模块即可恢复服务——其余模块
（jwxt_core / jwxt_state / jwxt_http / school_endpoints）一律只引用这里的常量，
不得复制字面量；tests/test_school_contract.py 的反重复扫描会强制这一点。

取证约定：
- 每条事实注明取证日期与重验方法（如何从学校公开页面重新发现它）；
- 变更事实时同步更新 SCHOOL_CONTRACT_VERSION 与 docs/upstream-contract.md。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 契约版本：学校侧升级后按取证结果递增（格式：日期.序号）
# ---------------------------------------------------------------------------
SCHOOL_CONTRACT_VERSION = "2026-11-14.01"

# ---------------------------------------------------------------------------
# 层标识
# ---------------------------------------------------------------------------
LAYER_WEBVPN = "webvpn"         # WebVPN 边缘（URL 加密 + Cookie 桥）
LAYER_CAS = "cas"               # 统一身份认证（RSA 加密票据）
LAYER_PORTAL = "portal"         # 门户前端（登录参数/应用列表/会话校验）
LAYER_MGMT = "jwxt-mgmt"        # 教务系统管理端（SSO + 系统参数）
LAYER_STUDENT = "jwxt-student"  # 教务系统学生服务（成绩/课表/学籍 JSON API）

# 对外公开健康契约只暴露四个粗粒度层；管理端/学生服务归并为 jwxt
PUBLIC_LAYERS = ("webvpn", "cas", "portal", "jwxt")


def coarse_layer(layer: str) -> str:
    """内部层名 → 对外粗粒度层名（公开契约不含内网端口等细节）。"""
    if layer in (LAYER_MGMT, LAYER_STUDENT):
        return "jwxt"
    return layer


# ---------------------------------------------------------------------------
# 第一层：WebVPN 边缘（2026-08 取证）
#   重验方法：浏览器登录学校 WebVPN，观察地址栏形态与 /webvpn/cookie/ 接口
# ---------------------------------------------------------------------------
VPN_BASE = "https://sec.stpt.edu.cn"        # WebVPN 入口（旧名 BASE）
VPN_HOST_WITH_PORT = "sec.stpt.edu.cn:443"  # ent() URL 加密的盐输入（旧名 RUMP_HOST）
VPN_PUBLIC_HOST = "sec.stpt.edu.cn"         # Cookie/DNS 白名单使用的主机名
WEBVPN_URL_PREFIX = "/webvpn/"              # 加密 URL 前缀
WEBVPN_COOKIE_PATH = "/webvpn/cookie/"      # 虚拟 Cookie 写入/读回接口
DNS_FALLBACK_HOSTS = ("sec.stpt.edu.cn",)   # DNS 兜底白名单

# WebVPN 对内网 host:port 的查询串映射后缀（api/...?vpn-12-<host>&_t=...）
VPN_QUERY_MARK = "vpn-12-"

# 会话 Cookie 保留域白名单：查询请求只走这两个主机（其父域 Cookie 也发送）
SESSION_COOKIE_HOSTS = ("sec.stpt.edu.cn", "cas.stpt.edu.cn")


def vpn_suffix(host: str) -> str:
    """组合 WebVPN 主机映射查询后缀，如 vpn-12-10.1.200.4:9995。"""
    return VPN_QUERY_MARK + host


def is_webvpn_url(url) -> bool:
    """URL 是否已是 WebVPN 加密形态（学生门户地址还原时的判定）。"""
    return str(url or "").startswith(VPN_BASE + WEBVPN_URL_PREFIX)


# ---------------------------------------------------------------------------
# 第二层：CAS 统一身份认证（2026-08 取证）
#   重验方法：从门户 redirectToCas 页面提取 CAS 地址，打开登录页观察表单与 JS
# ---------------------------------------------------------------------------
CAS_COOKIE_DOMAIN = "cas.stpt.edu.cn"
CAS_VPN_HOST = "cas.stpt.edu.cn"               # 票据端点的 WebVPN 主机映射后缀
CAS_LOGIN_PATH = "/lyuapServer/login"          # 登录页（含 RSA 公钥 JS 引用）
CAS_TICKETS_PATH = "/lyuapServer/v1/tickets"   # 票据端点（一次返回 tgt+ticket）

# 登录页 RSA 公钥：公钥指数/模数从登录页引用的 bundle JS 中提取，
# 指数提取失败时使用历史公钥指数兜底（模数无兜底，必须实测）
CAS_RSA_EXPONENT_FALLBACK = 0x10001
CAS_BUNDLE_JS_RE = re.compile(r'src="([^"]*app\.[^"]+\.js[^"]*)"')
CAS_RSA_EXPONENT_RE = re.compile(r'public_exponent:"([0-9a-f]+)"')
CAS_RSA_MODULUS_RE = re.compile(r'modulus:"([0-9a-f]+)"')

# CAS 登录口令加密盐：loginUserToken 头 = RSA(盐 + 毫秒时间戳)
LOGIN_TOKEN_SALT = "lyasp"

# CAS 会话 Cookie：写回 WebVPN 虚拟 Cookie 域；值为 None 的项在登录时取动态值
# （TGT Cookie），其余为固定值
CAS_TGT_COOKIE_NAME = "CASTGC"
CAS_SESSION_COOKIES = ((CAS_TGT_COOKIE_NAME, None), ("session", "1"),
                       ("locale", "zh_CN"))

# 门户 redirectToCas 页面中 CAS 登录链接的提取正则
CAS_REDIRECT_URL_RE = re.compile(r'href="(https?://[^"]+?lyuapServer/login[^"]*)"')

# CAS 票据响应结构：v1/tickets 返回 {tgt, ticket}
CAS_TICKET_FIELDS = ("tgt", "ticket")


# ---------------------------------------------------------------------------
# 第三层：rump_frontend 门户前端（2026-08 取证）
#   重验方法：登录 WebVPN 后抓取门户首页 XHR（getLoginParam/getAppList 等）
# ---------------------------------------------------------------------------
PORTAL_PREFIX = "/rump_frontend/"
PORTAL_LOGIN_PARAM = PORTAL_PREFIX + "getLoginParam/"      # 登录参数（auth.master 判定）
PORTAL_REDIRECT_TO_CAS = PORTAL_PREFIX + "redirectToCas/"  # 返回跳 CAS 的页面
PORTAL_LOGIN_FROM_CAS = PORTAL_PREFIX + "loginFromCas/"    # ticket 换门户会话
PORTAL_HOME_PARAM = PORTAL_PREFIX + "getHomeParam/"        # 会话校验（比对学号）
PORTAL_APP_LIST = PORTAL_PREFIX + "getAppList/"            # 应用列表（发现教务地址）
PORTAL_LOGIN_PAGE = PORTAL_PREFIX + "login/"               # 门户登录页（预热直达）

# CAS service 参数：门户用 loginFromCas 作为 ticket 回收地址
PORTAL_SERVICE = VPN_BASE + PORTAL_LOGIN_FROM_CAS

# getLoginParam 响应结构：data.auth.master 必须为 cas（否则视为门户改版）
LOGIN_PARAM_EXPECTED_MASTER = "cas"

# getAppList 响应结构：sites 分组内 name == 该应用名者即教务管理端地址
MGMT_APP_NAME = "教务系统"


# ---------------------------------------------------------------------------
# 第四层：教务系统（2026-08 取证）
#   重验方法：经管理端 SSO 后抓取 sysSet 参数（XSFWDZ=学生服务地址），
#   再从学生服务 SPA 的 JS 包内枚举 api/ 接口
# ---------------------------------------------------------------------------
MGMT_HOST = "10.1.200.4"
MGMT_PORT = 9090
MGMT_VPN_HOST = "%s:%d" % (MGMT_HOST, MGMT_PORT)    # 主机映射后缀用
STUDENT_PORT = 9995
STUDENT_VPN_HOST = "%s:%d" % (MGMT_HOST, STUDENT_PORT)

# 管理端 SSO pattern（api/cas/login?pattern=manager-login&ticket=...）
MGMT_SSO_PATTERN = "manager-login"
# 管理端 SSO service hint：跳转 Location 提取失败时的回退值
MGMT_SSO_SERVICE = "http://%s:%d/%s?pattern=%s" % (
    MGMT_HOST, MGMT_PORT, "api/cas/login", MGMT_SSO_PATTERN)
MGMT_CAS_LOGIN_PATH = "api/cas/login"
# 经 api/cas/login 302 Location 头提取管理端 CAS service 参数的正则
SSO_SERVICE_LOCATION_RE = re.compile(r"service=([^&]+)")
# 管理端系统参数接口：paramCode=学生服务地址参数码 的值即学生服务原始地址
MGMT_SYSSET_PATH = "api/system/sysSet/selectByParamCode"
SYSSET_PARAM_CODE = "XSFWDZ"
SYSSET_VALUE_FIELD = "paraValue"    # sysSet 响应中承载参数值的字段名

# 学生服务/管理端 JSON API 的 csrfToken 盐：md5(时间戳 + 盐)
CSRF_SALT = "lyedu"


# ---------------------------------------------------------------------------
# 学校端「会话已失效」信号（2026-08 取证，随学校文案变化需重验）
#   命中后视为认证类失败：不进负缓存、按 401 返回并清除内存会话，
#   避免把「强制下线/未登录」误当成瞬时上游故障反复重试。
# ---------------------------------------------------------------------------
SESSION_INVALID_CODES = {
    "53000010", "53000011", "53000012",
    "53010010", "53010011", "53010012",
}
SESSION_INVALID_HINTS = (
    "未登录", "未登陆", "已在其他地方登录", "强制下线",
    "会话已失效", "登录已过期", "请重新登录",
)


# ---------------------------------------------------------------------------
# 路径组合助手：注册表与调用方统一使用，保证与历史 URL 逐字节一致
# ---------------------------------------------------------------------------
def api_path(path: str, vpn_host: str = "", leading: str = "",
             trailing: str = "", with_ts: bool = True) -> str:
    """组合「相对路径 + 查询串」的完整路径模板。

    组合顺序与学校侧历史 URL 逐字节一致：
      path?[leading&]vpn-12-<host>[&trailing][&_t=%d]
    - leading：置于主机映射后缀之前的参数模板（如 courseType={course_type}）
    - trailing：置于主机映射后缀之后的参数模板（如 paramCode=XSFWDZ）
    - with_ts：是否追加 &_t=%d 时间戳占位（由调用方在请求前替换）；
      时间戳历史上只伴随主机映射后缀出现，vpn_host 为空时不追加
    vpn_host 与参数均为空时不拼查询串（门户静态路径）。
    """
    if not vpn_host and not leading and not trailing:
        return path
    parts = [x for x in (leading,
                         vpn_suffix(vpn_host) if vpn_host else "",
                         trailing) if x]
    if with_ts and vpn_host:
        parts.append("_t=%d")
    return path + "?" + "&".join(parts) if parts else path

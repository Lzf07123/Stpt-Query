# 学校侧契约与升级重适配手册

> 本文回答一个问题：**学校教务系统升级后，如何最快恢复服务？**
> 契约事实的单一来源是 [app/school_contract.py](../app/school_contract.py)；
> 接口面的单一来源是 [app/school_endpoints.py](../app/school_endpoints.py)；
> tests/test_school_contract.py 用反重复扫描强制「改一处即可」。

## 1. 四层链路总览

```text
用户浏览器
  └─ frontend（nginx，唯一对外入口）
       └─ app（单体应用，内嵌查询代理）
            └─ WebVPN 边缘（sec.stpt.edu.cn，URL 加密 + Cookie 桥）
                 └─ CAS 统一身份认证（cas.stpt.edu.cn/lyuapServer，RSA 加密票据）
                      └─ rump_frontend 门户前端（登录参数/应用列表/会话校验）
                           └─ 教务系统（管理端 10.1.200.4:9090 + 学生服务 :9995）
```

| 层 | contract 常量前缀 | 职责 | 升级敏感度 |
| --- | --- | --- | --- |
| webvpn | `VPN_*`、`WEBVPN_*` | URL 加密（`ent()`，md5(RUMP_HOST) 派生盐）、跨域虚拟 Cookie 桥（`/webvpn/cookie/`）、`vpn-12-<host>` 查询串主机映射 | 换 VPN 域名/产品时高 |
| cas | `CAS_*`、`LOGIN_TOKEN_SALT` | 登录页 → bundle JS 提取 RSA 公钥 → `/lyuapServer/v1/tickets` 一次换 tgt+ticket；`loginUserToken = RSA("lyasp"+毫秒时间戳)` | 认证产品更换时高 |
| portal | `PORTAL_*`、`MGMT_APP_NAME` | `getLoginParam`（`auth.master=cas` 判定）、`redirectToCas`、`loginFromCas?ticket`、`getHomeParam` 会话校验、`getAppList`（按名称「教务系统」发现教务地址） | 门户改版时高 |
| jwxt | `MGMT_*`、`CSRF_SALT`、`SESSION_INVALID_*` | 管理端 SSO（`api/cas/login?pattern=manager-login`）→ `sysSet/selectByParamCode`（paramCode=XSFWDZ → 学生服务地址）→ 学生服务 JSON API（`csrfToken = md5(ts+"lyedu")`） | 教务系统升级时高 |

## 2. 契约事实清单与重验方法

学校侧升级后按下表逐项重验。所有事实变更**只改 school_contract.py**
（路径模板改 school_endpoints.py），改完跑测试与自检即可。

### webvpn 层

| 契约事实 | 常量 | 重验方法 |
| --- | --- | --- |
| 入口域名与 scheme | `VPN_BASE` | 浏览器登录学校 VPN，看地址栏 |
| URL 加密盐输入 | `VPN_HOST_WITH_PORT` | `ent()` 的盐 = `md5(该值)`；改后对比新站 URL 形态 |
| Cookie 桥路径 | `WEBVPN_COOKIE_PATH` | 浏览器开发者工具观察 `/webvpn/cookie/` 读写 |
| 查询串主机映射 | `VPN_QUERY_MARK`（`vpn-12-`） | 抓任一内网 API 请求的 query string |
| 会话 Cookie 保留域 | `SESSION_COOKIE_HOSTS` | 对照查询请求实际发送的 Cookie 域 |

### cas 层

| 契约事实 | 常量 | 重验方法 |
| --- | --- | --- |
| 登录页路径 | `CAS_LOGIN_PATH` | 从门户 `redirectToCas` 页面提取链接 |
| 票据端点 | `CAS_TICKETS_PATH` | 登录抓包（响应含 tgt+ticket） |
| bundle JS 定位 | `CAS_BUNDLE_JS_RE` | 打开登录页看 `<script src=...>` 命名 |
| RSA 公钥提取 | `CAS_RSA_EXPONENT_RE` / `CAS_RSA_MODULUS_RE` | 下载 bundle JS，搜索 `public_exponent` / `modulus` |
| 公钥指数兜底 | `CAS_RSA_EXPONENT_FALLBACK`（0x10001） | 与 JS 内提取值对照（模数无兜底，必须实测） |
| 登录口令盐 | `LOGIN_TOKEN_SALT`（`lyasp`） | 抓 `loginUserToken` 头，逆推格式（盐+毫秒时间戳） |
| 会话 Cookie 名 | `CAS_TGT_COOKIE_NAME`、`CAS_SESSION_COOKIES` | 登录后看 `/webvpn/cookie/?domain=...` 读回值 |
| 门户→CAS 链接正则 | `CAS_REDIRECT_URL_RE` | `redirectToCas` 页面源码 |

### portal 层

| 契约事实 | 常量 | 重验方法 |
| --- | --- | --- |
| 五个 XHR 路径 | `PORTAL_LOGIN_PARAM` 等 | 登录门户抓 XHR |
| 认证方式判定 | `LOGIN_PARAM_EXPECTED_MASTER`（`cas`） | `getLoginParam` 响应 `data.auth.master` |
| 应用列表结构 | `MGMT_APP_NAME`（「教务系统」） | `getAppList` 响应 `sites` 分组内按名称找教务 |
| 会话失效文案/错误码 | `SESSION_INVALID_HINTS` / `SESSION_INVALID_CODES` | 被顶下线/未登录时的响应文本与 code |

### jwxt 层（教务系统）

| 契约事实 | 常量 | 重验方法 |
| --- | --- | --- |
| SSO pattern | `MGMT_SSO_PATTERN`（`manager-login`） | 教务系统登录跳转 URL |
| 学生服务地址参数码 | `SYSSET_PARAM_CODE`（`XSFWDZ`）+ `SYSSET_VALUE_FIELD`（`paraValue`） | 管理端 `sysSet/selectByParamCode` 响应 |
| csrf 盐 | `CSRF_SALT`（`lyedu`） | 抓学生服务请求头 `csrfToken`，对照 `md5(ts+盐)` |
| 业务 API 路径 | `school_endpoints.py` 注册表 | 见第 4 节「接口面发现」 |

## 3. 升级应急流程

1. **定位漂移层**：
   - 看公开页状态条 / `/health/public` 的 `school.layers`（webvpn/cas/portal/jwxt 四层 up/down/unknown）；
   - 查询代理 `/health`（Bearer）看 `school.layers` 与 `deep.layers`；
   - 运行自检脚本（见第 5 节），得到逐条契约事实判定。
2. **取证**：按第 2 节「重验方法」用浏览器/抓包拿到新事实。无凭据探测
   （`probe_layers`）覆盖 webvpn/cas/portal；教务层需要真实账号（`health/deep`）。
3. **改契约**：只改 `app/school_contract.py`（或路径类改 `school_endpoints.py`），
   更新 `SCHOOL_CONTRACT_VERSION` 与本表。
4. **验证**：
   ```bash
   docker compose config --quiet
   docker build -q -t edu-query-app:test .
   docker run --rm -v "$PWD":/app -w /app edu-query-app:test \
     sh -c "pip install -q pytest pytest-asyncio && python -m pytest -q"
   python scripts/contract_selfcheck.py --json        # 或容器内执行
   ```
5. **部署后**：用真实账号跑一次 `POST /health/deep`，确认 `ok=true` 且
   `layers.jwxt.ok=true`。

反重复扫描（`test_school_literals_live_only_in_contract` /
`test_host_and_protocol_facts_live_only_in_contract`）保证事实不会被复制到
契约之外——若扫描失败，说明有人把字面量写进了别的模块，先收敛再继续。

## 4. 接口注册表与新增端点

`school_endpoints.py` 已登记全部已探明端点（门户 6 + WebVPN 3 + CAS 3 +
管理端 3 + 学生服务 8）——现有查询链路（成绩/课表/免密跳转）的调用事实。
如需新增查询端点，按以下步骤接入：

1. **登记**：在 `school_endpoints.py` 增加 `EndpointSpec`
   （layer/base/method/path/查询参数模板），路径模板用 `{param}` 占位。
2. **调用**：业务代码用 `J.call(name, body=..., **params)`——自动带
   AJAX+csrfToken 头、会话失效检测、默认解包 `data` 字段；
   导出/下载类传 `raw=True` 拿原始 Response。
3. **渲染与分类**：输出渲染进 `render.py`（单一事实来源），异常文案进
   `classifier.py`（单一事实来源），并在 `_CATEGORY_LAYER` 标注新类别归属层。
4. **写操作注意**：选课/评教等写接口必须单独限流与审计，先与学校确认授权。

## 5. 契约自检工具

```bash
# 无凭据（不登录，只读 GET）：WebVPN/门户/CAS 三层 + RSA 公钥提取判定
python scripts/contract_selfcheck.py
python scripts/contract_selfcheck.py --json     # 机器可读，退出码 0/1 可接入 CI

# 深度（真实登录链路，验证教务层；凭据走环境变量，不落盘不打印）
JWXT_HEALTH_USERNAME=... JWXT_HEALTH_PASSWORD=... python scripts/contract_selfcheck.py --deep

# 容器内（与生产同环境）
docker compose exec edu-query-app python scripts/contract_selfcheck.py --json
```

输出包含：三层结论（ok/延迟/HTTP 状态）、逐条契约事实判定（通过/漂移/未判定）、
`--deep` 时附真实登录链路逐阶段结果与教务层结论。退出码 1 表示发现漂移证据。

## 6. 分层健康契约（运行时观测）

| 接口 | 字段 | 说明 |
| --- | --- | --- |
| `/health`（Bearer） | `school.layers.{webvpn,cas,portal}` | 来自无凭据分层探测（`JWXT_LAYER_PROBE_INTERVAL>0` 时后台周期刷新，或 health/deep 实测后缓存） |
| `/health`（Bearer） | `school.layers.jwxt`、`deep.layers.jwxt` | 来自最近一次 `deep_check`（三态：true/false/null=未测） |
| `/health`（Bearer） | `contract_version` | 当前生效的契约版本 |
| `/health/public` | `school.layers.*` | 上述结果的粗粒度投影（up/down/unknown，不含内部地址） |
| `classify_error().meta.layer` | `cas`/`portal`/`jwxt`/`null` | 异常的粗归因（精确定位用自检脚本） |

`deep_check` 的 stages 每条含 `{stage, at_ms, ok, layer, error?}`，失败时直接
指出漂移阶段与层；`layers` 汇总四层三态结论（登录即败时 jwxt=null 不误报）。

## 7. 契约版本历史

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| 2026-11-14.01 | 2026-11-14 | 契约模块建立；21 端点登记入册；无凭据自检 7 项事实全数实测通过 |

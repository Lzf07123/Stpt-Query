# 任务书 01：教务系统接口面侦察（复刻阶段 0）

> 派活依据：`AGENTS.md` 第四节（边界明确、Consumes/Produces 精确到文件、验收可独立验证）。
> 执行者接单后先读：`AGENTS.md` 全文（尤其第三节规则 5/10/11、第六节流程 B/C）、
> `docs/upstream-contract.md`、`app/school_contract.py`、`app/school_endpoints.py`。

## 1. 任务目标

用真实会话一次性枚举学生服务（内网 :9995，经 WebVPN 映射）SPA 的**完整 REST 接口面**，
产出「接口面地图」文档与注册表登记，为后续逐模块复刻（考试安排/学籍卡/培养方案/评教…）
提供完整事实基础。当前只探明 8 个学生服务端点（成绩/课表/学分/学籍/学期），
教务系统其余模块的接口面完全未知——本任务把未知变成已知。

## 2. 背景（已确认的项目事实，无需再调查）

- 认证编排已全部建成：`app/jwxt_core.py` 的 `J` 类可 `login() → jwxt() → student()`，
  之后 `J.stu` 即学生服务基址（WebVPN 加密形态），`J.api(method, path, ts, body)`
  自动带 AJAX + csrfToken 头（csrf = md5(ts + `contract.CSRF_SALT`)）。
- 学生服务是 hash 路由 SPA（`stu_src` 带 `#/` 片段）。**SPA 的 JS chunk 内含全部
  `api/...` 调用字符串**——静态分析 bundle 即可枚举端点，无需逐菜单点击、无需抓包工具。
- 端点命名规律：`api/<域>/<控制器>/<方法>`；已见域：`baseInfo`、`score`、`student`、
  `arrange`、`system`。查询串携带 WebVPN 主机映射后缀
  `?vpn-12-<STUDENT_VPN_HOST>&_t=<时间戳>`（组合助手 `contract.api_path`）。
- 已探明的 8 个学生服务端点见 `app/school_endpoints.py` 的 `student.*` 条目，
  作为新登记条目的格式黄金样例。
- 无凭据可达性：`scripts/contract_selfcheck.py` 当前实测 webvpn/cas/portal 三层全过；
  学生服务层需要真实会话（本任务的凭据用途）。

## 3. 交付物（Produces，精确到文件）

| # | 文件 | 内容要求 |
| --- | --- | --- |
| 1 | `scripts/discover_student_api.py` | 侦察工具（详见第 4 节规格） |
| 2 | `docs/upstream-api-map.md` | 接口面地图：端点总数、按域分组表（路径/方法/参数线索/推断用途）、与已登记 8 端点的比对结论（新增/已登记/疑似写操作） |
| 3 | `app/school_endpoints.py` | 登记「高置信只读端点」为 `EndpointSpec`（layer=STUDENT、base=student、needs_csrf=True、with_ts=True、vpn_host=STUDENT_VPN_HOST）；**疑似写操作端点只登记不实现**，description 以 `write-op` 前缀标注，留待授权流程 |
| 4 | `tests/test_school_contract.py` | 黄金字符串测试补充：新登记端点的 `render()` 输出与侦察抓到的 URL 逐字节对照 |
| 5 | `tests/test_api_discovery.py` | 侦察工具的纯函数测试（fixture JS 片段 → 提取结果），不触网 |

## 4. 侦察工具规格（scripts/discover_student_api.py）

- **凭据**：`--username/--password` 或环境变量 `JWXT_HEALTH_USERNAME/PASSWORD`；
  不落盘、不打印、不进日志（硬性规则 4）；无凭据时退出码 2 + 中文提示。
- **流程**（复用既有编排，禁止重写认证）：
  1. `J(u, p)` → `login() → jwxt() → student()`（复用 `jwxt_core.J`）；
  2. GET `J.stu` 的 index.html → 提取全部 `<script src=…>`（相对与绝对路径）；
  3. 逐个 GET chunk JS（经 WebVPN 前缀，串行、无并发——对学校友好）；
  4. 正则提取 `api/...` 调用串 + 相邻的 HTTP 方法线索（axios 风格
     `.get(` / `.post(` / `url:"…"` / `request({url:`）；
  5. 按域分组去重；`--json` 输出机器可读清单；`--out` 生成地图文档草稿；
  6. 与 `school_endpoints.endpoint_names()` 比对，标记 已登记/新增/疑似写操作。
- **可测试性**：提取逻辑必须纯函数化（输入 JS 文本 → 输出端点列表），单测用
  fixture 字符串（含 axios 各种写法样本），全程不触网；登录/GET 部分薄封装不测。
- **失败语义**：登录链路异常直接抛出并中文提示（不吞错）；发现 `auth master` /
  门户结构漂移信号时，停下按 `AGENTS.md` 第六节 C 先修契约再继续。

## 5. 依赖（Consumes）

- 真实学号/密码（**仅最终运行侦察脚本需要**，由任务发起人提供；禁止写入任何
  文件、提交、记忆库或日志）；
- `app/jwxt_core.py` 的 `J` 类与并发框架现状（只读复用）；
- `app/school_contract.py` / `app/school_endpoints.py` 当前版本（契约版本
  `2026-11-14.01`，自检 7 项事实全过）；
- 学校侧当日可达（运行前先跑 `scripts/contract_selfcheck.py --json` 确认退出码 0）。

## 6. 验收标准（可独立验证）

1. `docker run --rm -v "$PWD":/app -w /app edu-query-app:test sh -c "pip install -q pytest pytest-asyncio && python -m pytest -q"`
   全量通过，基线 295+3 不回退，新增测试全绿；
2. 反重复扫描（`test_school_literals_live_only_in_contract`）通过：学校字面量
   只存在于契约/注册表两文件；
3. `python scripts/discover_student_api.py --help` 可用；无凭据退出码 2；
4. 真实运行后 `docs/upstream-api-map.md` 存在且含四列比对结论；
5. 新登记端点全部有黄金字符串测试；`student.*` 注册表条目数增加且
   `test_registry_names_unique_and_layers_valid` 通过；
6. 脚本对学校只发 GET（登录握手 + 页面/JS 拉取），零写操作、零并发。

## 7. 边界与禁做

- **不实现任何业务模块**（考试安排等功能 = 后续任务书 02+）；
- 不修改认证编排（`jwxt_core` 的 `J` 类）；遇登录链路异常 → 停下回报，不打补丁；
- 不把侦察输出中的个人信息（姓名/学号/成绩）写入文档、测试或提交信息；
- 不动未提交的工作区改动（当前工作区含契约基建 + 语义统一两轮改动，均未提交）。

## 8. 后续任务链（本任务书不含）

| 任务 | 内容 | 前置 |
| --- | --- | --- |
| 02 | 首个新模块 `/api/v1/exams`（考试安排）——走 `AGENTS.md` 第六节 B 流程 | 任务 01 的地图 |
| 03 | 学籍卡 / 培养方案模块 | 任务 02 沉淀的模式 |
| 04 | 写操作（选课/评教）——需学校授权，单独评审 | 任务 01 标注的 write-op 清单 |

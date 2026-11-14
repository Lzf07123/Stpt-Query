# 接口语义规范（/api/v1/*）

> 单一事实来源：[app/api_semantics.py](../app/api_semantics.py)。
> 所有 `/api/v1/*` JSON 接口的响应信封、稳定错误码、meta 与分页语义都由该模块定义；
> tests/test_api_semantics.py 锁定契约。

## 1. 响应信封

所有 `/api/v1/*` JSON 响应（成功与失败）统一为五键信封：

```json
{
  "success": true,
  "code": "OK",
  "message": "",
  "data": { },
  "meta": { "trace_id": "…", "elapsed_ms": 123 }
}
```

| 字段 | 语义 |
| --- | --- |
| `success` | 布尔：本请求是否成功完成 |
| `code` | 稳定机器码：`OK` 或错误码注册表中的条目；前端据此编程化判断，禁止再匹配错误文案 |
| `message` | 人类可读中文摘要（可直接展示） |
| `data` | 业务载荷；失败时携带错误上下文（如 `diagnosis` 诊断报告） |
| `meta` | 元信息：`trace_id`（贯穿链路的运行 ID）、`elapsed_ms`、`layer`（学校契约层归因）、`category`（classifier 分类）、`lock_until`、`retry_after`、`count`、`calendar` 等 |

**HTTP 状态码语义**：业务失败使用真实状态码（4xx/5xx），不再 `200 + success:false`；
请求体校验错误沿用 FastAPI 约定的 422。

## 2. 稳定错误码注册表

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `request.invalid` | 400（校验错误 422） | 参数缺失/非法、请求体校验失败 |
| `request.unauthorized` | 401 | 网关令牌缺失或错误 |
| `request.forbidden` | 403 | 无权访问 |
| `request.not_found` | 404 | 接口/资源不存在 |
| `request.method_not_allowed` | 405 | 方法不支持 |
| `request.rate_limited` | 429 | 限流命中 |
| `request.timeout` | 504 | 查询总预算超时 |
| `capacity.concurrency_full` | 503 | 全局查询并发满 |
| `capacity.queue_full` | 503 | 异步任务排队满 |
| `capacity.queue_unavailable` | 503 | 排队后端故障 |
| `capacity.jobs_disabled` | 503 | 未配置 REDIS_URL（异步未启用） |
| `school.cas.invalid_credentials` | 401 | 账号或密码错误 |
| `school.cas.locked` | 403 | 学校风控/账号锁定（`meta.lock_until`） |
| `school.session.expired` | 401 | 学校端会话失效 |
| `school.upstream.busy` | 503 | 学校上游繁忙 |
| `school.upstream.unavailable` | 502 | 上游瞬时故障（网络/连接） |
| `school.upstream.error` | 502 | 上游业务错误（5xx 等） |
| `school.portal.changed` | 502 | 门户结构漂移（`auth master` 信号，升级排查入口） |
| `school.jwxt.semester_closed` | 409 | 课表学期未开放 |
| `service.unavailable` | 503 | 服务自身暂时不可用 |
| `internal.error` | 500 | 未分类内部错误 |

映射关系：classifier 的 `meta.category` 是分类唯一事实来源，`code_for_category()`
只做 category→code 映射；日历模块自有码经 `code_for_calendar()` 归一；
横切依赖抛出的裸 HTTPException 经 `code_from_http_status()` 兜底归一。

## 3. 分页与列表语义

列表类端点的分页统一为 `data.pagination = {total, offset, limit, has_more}`；
service-status 的统计统一收进 `data.stats`（原顶层歧义的 `success` 计数更名为
`success_count`）。

## 4. 查询主链路（POST /api/v1/run）

- 请求体（WorkflowRequest）：`username` / `password` / `option`（`成绩`|`课表`）/
  `semesters` / `weeks` / `md2pdf` / `check` / `include_all` / `odd_or_double` / `user`。
  `semesters` 支持空（当前学期）、单学期、`all`（全部学期）、`recent:N`（最近 N 学期）
  与逗号分隔多学期（多学期形态仅成绩适用，上限由查询代理约束）；
  `include_all`（仅成绩）附带全部成绩与学分概况；`odd_or_double`（仅课表，`1`/`2`）
  透传学校单双周原始语义，留空沿用代理默认 `1`；
- 成功：`data = {kind, output, pdf_base64?}`；`meta = {trace_id, elapsed_ms, count,
  response_summary, analysis_used, analysis_usage, calendar?, ...}`
- 失败：`data = {kind, diagnosis: {report, user_actions, admin_actions,
  response_summary, status_code}}`；`meta.layer/category/lock_until` 承载归因
- 异步任务：`POST /api/v1/run/jobs` 受理（`data.job_id/poll_url`）、
  `GET /api/v1/run/jobs/{id}` 轮询（`data.state/phase/...`；终态 `data.result`
  为同一信封形状的嵌套载荷）

## 5. 路径迁移映射（不再兼容 Dify）

| 旧路径（已删除） | 新路径 |
| --- | --- |
| `POST /run`、`POST /v1/workflows/run` | `POST /api/v1/run` |
| `POST /run/jobs`、`GET /run/jobs/{id}` | `POST /api/v1/run/jobs`、`GET /api/v1/run/jobs/{id}` |
| `GET /notices/active`、`/notices/history` | `GET /api/v1/notices/active`、`/api/v1/notices/history` |
| `GET /service-status` | `GET /api/v1/service-status` |
| `GET /query-logs` | `GET /api/v1/query-logs` |
| `GET /health/public` | `GET /api/v1/status` |
| `GET/POST/PATCH/DELETE /admin/api/*` | `GET/POST/PATCH/DELETE /api/v1/admin/*` |

## 6. 豁免清单（不套信封）

以下端点保持各自的协议语义：

- `GET /health/live`、`GET /health/ready`、`GET /health` — K8s/容器探针
- `GET /metrics` — Prometheus 文本
- `GET /cal/{token}.ics` — 日历订阅源（ETag/304）
- `GET /jump/go`、`GET /get_schedule/export` — 302 桥接 / PDF 二进制下载
- `GET /`、静态页面 — HTML
- 内嵌查询代理（进程内 ASGI 的 `/login`、`/get_grades` 等）— 内部契约，不对公网暴露

## 7. 前端契约要点

- 查询页以 `code` 判断降级（`capacity.jobs_disabled`/`capacity.queue_unavailable`
  → 回退同步查询），不再匹配 503 detail 文案；
- 锁定卡片读 `meta.lock_until`；诊断报告读 `data.diagnosis.report`；
- 运行 ID 读 `meta.trace_id`；PDF 读 `data.pdf_base64`；
- 后台 401/403：`code` 以 `school.` 开头时展示学校侧原因，否则视为后台凭据无效。

## 8. 新接口接入清单

新增接口/教务模块/学校升级的标准流程以 `AGENTS.md` 第六节为准（项目宪法，
含逐项 checklist）；本文是语义规范本体，两者冲突时以 `app/api_semantics.py`
代码与本文为准，并回改 AGENTS.md。

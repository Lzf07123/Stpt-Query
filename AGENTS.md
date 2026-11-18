# edu-query-app 项目协作手册（多 Agents）

> 本文件是给后续 AI Agent（Codex 等）的「项目宪法」。**新会话必须先完整读完本文件与 README 再动手；子 agent 接单后同样先读本文件与第二节事实来源。** 人类开发者同样适用。

## 一、项目是什么

edu-query-app 是**单容器双进程（nginx 边缘入口 + uvicorn 应用进程）**的教务查询服务：

- **edu-query-app（单容器）**：容器内 nginx（8080 唯一对外入口，托管查询页与后台、反向代理 `/api/v1/*`——注入网关令牌，`/api/v1/admin/*` 原样透传管理员凭据——与 `/health*`、`/jump/go`、`/get_schedule/export`、`/cal/`）+ uvicorn（仅容器内回环 8000：学校统一认证登录、免密跳转、成绩/课表原始查询（内嵌查询代理，以 ASGI 方式同进程调用）、固定工作流编排、Markdown 渲染、成绩分析 LLM、异常分类与 PDF）。`frontend/` 目录保留为 nginx 资产目录（静态页/模板/入口脚本片段），构建期并入单镜像。
- **对外契约**：全部 JSON 接口统一 `/api/v1/*` 命名与统一语义（信封 + 稳定错误码，见 `docs/api-semantics.md`）；**不再兼容 Dify**，旧路径（/run、/v1/workflows/run 等）已删除。
- **上游契约**：学校侧四层链路（WebVPN → CAS → 门户 → 教务系统）的全部事实集中在契约模块，学校升级时改一处即可恢复（见第六节标准流程）。

## 二、事实来源

动手前先读以下文件，禁止用猜测代替调查：

- `README.md`：架构、契约与使用方式。
- `docker-compose.yml`：单容器（nginx + uvicorn 双进程）的端口与限额唯一事实（唯一宿主映射 `${APP_PORT:-8000}` → 容器内 nginx `8080`；uvicorn 仅容器内回环）。
- `app/`：单体应用代码事实（编排、渲染、内嵌查询代理 `app/jwxt_*.py`）。
- `frontend/`：页面与 nginx 模板事实。
- `design-system/edu-query-app/`：项目内品牌方案（BRAND/MASTER），前端视觉决策的唯一事实来源。
- `tests/`：行为契约（渲染、分类、编排、HTTP、查询日志、安全与过载保护、**接口语义**、学校侧契约）。
- `app/api_semantics.py`：**统一接口语义单一来源**（/api/v1/* 信封、稳定错误码、分页）；规范全文见 `docs/api-semantics.md`。
- `app/school_contract.py`：**学校侧事实单一来源**（域名/路径/端口/盐/正则）；`app/school_endpoints.py`：**上游接口注册表**（已探明端点；新增查询端点的唯一挂载点）。
- `docs/api-semantics.md`：接口语义规范（信封/错误码表/迁移映射/豁免清单/前端契约要点）。
- `docs/upstream-contract.md`：学校侧契约取证方法与升级重适配手册（含 `scripts/contract_selfcheck.py` 自检工具用法）。
- `docs/kubernetes-migration.md`：未来 K8s 迁移映射。
- `Li-Design/`：Git 子模块，**仅作设计/README 规范参考，非运行时依赖**。

## 三、硬性规则

1. **单体应用边界**：查询代理与编排合并为 `app/` 单一应用（整体搬迁、不得残留副本）；查询代理以进程内 ASGI 方式调用，禁止新增跨服务 HTTP 调用或绕过本仓库恢复兄弟仓库构建依赖。
2. **默认唯一对外入口**：单容器内 nginx 监听 8080 并映射宿主 `${APP_PORT:-8000}`，uvicorn 仅绑定容器内回环 `127.0.0.1:8000`；内嵌查询代理无独立端口、无对外网络。需要直连 uvicorn 做验收时用 `docker compose exec edu-query-app curl http://127.0.0.1:8000/...`（容器内回环），不得将回环端口发布到宿主或公网，公网直连必须先补 TLS、防火墙、访问控制与安全验收。
3. **应用持久状态仅两项**：①全站通知运营配置（JSONL 权威存储 + 可选 Redis 恢复副本）；②日历订阅（数据落本地库，学校凭据仅以 AES-GCM 密文存储）。其余查询业务状态不落盘，PDF 以 Base64 内联返回。两项持久状态均要求单实例部署并纳入备份。
4. **令牌安全**：固定 `API_TOKEN`（前端与后端共用）；nginx 反代时注入 `Authorization`；页面不得持有令牌；日志与响应不得输出密码/session/token。
5. **单一事实来源**：提示词只在 `prompts.py`、异常规则只在 `classifier.py`、渲染逻辑只在 `render.py`、接口语义（信封/错误码/分页）只在 `api_semantics.py`、学校侧事实只在 `school_contract.py`、上游端点只在 `school_endpoints.py`、环境变量文档只在 `.env.example`，禁止重复实现。
6. **命名连字符**：服务与镜像一律连字符（单容器 `edu-query-app`）；Python 内部模块沿用 `app/jwxt_*.py` 既有命名。
7. **完成 = 验证 + 文档**：声称完成前跑第五节验证命令并保留输出；涉及结构/契约变更时同步 README、`.env.example` 与 `docs/api-semantics.md`。
8. **不做破坏性操作**：不执行 `rm -rf`、force push、删除他人提交；不动未提交的改动。
9. **日历订阅 URL 属长期密钥**：禁止出现在日志、记忆库、提交信息与通知中；服务端只存 token 哈希 + 加密副本，必须支持轮换、撤销与到期，`/cal/` 关闭访问日志。
10. **统一接口语义（/api/v1/\*）**：
    - 所有新增对外 JSON 接口必须挂在 `/api/v1/*` 命名空间（管理端 `/api/v1/admin/*`，凭据原样透传不注入网关令牌）；
    - 响应一律走 `api_semantics` 信封 `{success, code, message, data, meta}`——成功用 `ok_envelope`，失败抛 `ApiError` 或返回 `error_envelope`；**禁止 200 + success:false 的旧形态**；
    - 错误码必须已在 `api_semantics.CODES` 注册；新场景先登记码再使用，码取值 `<域>.<原因>`；
    - 业务失败使用真实 HTTP 状态码（凭据 401、锁定 403、参数 400/422、上游 502/503、超时 504）；
    - 前端与测试判断错误**一律用 `code` 字段**，禁止匹配错误文案/正则；`detail` 字段属历史遗留，禁止在新代码中读写；
    - 豁免清单（`/health/live`、`/health/ready`、`/health`、`/metrics`、`/cal/{token}.ics`、`/jump/go`、`/get_schedule/export`、页面 HTML、内嵌代理内部契约）保持各自协议语义，不套信封、不新增豁免除非用户明确要求。
11. **学校侧契约（上游对接的单一事实来源）**：
    - 学校侧事实只允许出现在 `school_contract.py`（主机/端口/盐/正则）与 `school_endpoints.py`（路径模板）；`tests/test_school_contract.py` 的反重复扫描会强制拒绝其他文件中的字面量；
    - 调用上游一律用注册表 + `J.call(endpoint, ...)`，禁止手写 URL 字符串；
    - 学校侧升级时按第六节流程走：自检定位 → 改契约一处 → 全量验证，不做局部打补丁；
    - 新增写操作接口（选课/评教等）必须先与学校确认授权，并单独限流与审计。

## 四、多 Agents 协作规范

总原则：**单一事实来源、一个任务一个 owner、并行任务零文件重叠。**

派活规则：

1. 只派具体、有边界的 Task；附完整上下文，确保子 agent 无需猜测项目事实。
2. Task 写清 Consumes（依赖/输入文件）与 Produces（产出文件/契约），精确到文件，验收标准可独立验证。
3. 并行派发的 Task 不得重叠同一文件或同一契约；有依赖关系一律串行。
4. 共享工作区改动即时可见，先认领再动手。

执行纪律：

- 动手前先读本文件与第二节事实来源；不清楚就问 root，不许用猜测代替调查。
- 不擅自扩大任务范围；需要新决策时回报 root 或停下询问用户。
- 验证才算完成：跑第五节命令并保留输出；失败必须说明原因与证据。
- 遇阻先探原因（读文件、跑最小检查、对比历史提交），带着证据汇报，不静默停摆。

## 五、验证命令

```bash
# 1. 编排配置合法
docker compose config --quiet

# 2. 单元测试（当前基线 295 通过 + 3 跳过，跳过为容器内无 node 的前端片段用例）
docker build -q -t edu-query-app:test .
docker run --rm -v "$PWD":/app -w /app edu-query-app:test \
  sh -c "pip install -q pytest pytest-asyncio && python -m pytest -q"

# 2c. 学校侧契约自检（无凭据只读探测；发现漂移证据时退出码 1）
python scripts/contract_selfcheck.py --json   # 或：docker compose exec edu-query-app python scripts/contract_selfcheck.py --json

# 2b. 前端令牌构建（改动 frontend/src/ 或 brand.js 后必须重编译）
cd frontend && npm install && npm run build && cd ..
# 查询页脚本已外置为 static/app.js（首屏主题预置为 static/theme-init.js），
# 后台 admin.html 的主题预置复用同一 theme-init.js 单源。
# 全站已无内联脚本，CSP 的 script-src 只走 'self'，无需维护任何内联哈希；
# 资产改动后同步 bump 页内 ?v=N 版本号与 sw.js 的 VERSION。

# 3. 单容器端到端冒烟（需 Docker daemon；含统一语义验收）
API_TOKEN=smoke-token AUTO_ROTATE_TOKEN=false docker compose up -d --build
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/health/ready
# 信封验收：状态/通知端点必须返回五键信封
curl -s http://127.0.0.1:8000/api/v1/status | grep -o '"code":"OK"'
curl -s http://127.0.0.1:8000/api/v1/notices/active | grep -o '"code":"OK"'
# 错误码验收：参数校验 422、异步未启用 503、旧路径 404（不再兼容 Dify）
curl -s -X POST http://127.0.0.1:8000/api/v1/run -H 'Content-Type: application/json' \
  -d '{"username":"abc","password":"pw","option":"成绩"}' | grep -o '"code":"request.invalid"'
curl -s -X POST http://127.0.0.1:8000/api/v1/run/jobs -H 'Authorization: Bearer smoke-token' \
  -H 'Content-Type: application/json' -d '{"username":"2023000001","password":"pw","option":"成绩"}' \
  | grep -o '"code":"capacity.jobs_disabled"'
curl -s -X POST http://127.0.0.1:8000/run -d '{}' | grep -o '"code":"request.not_found"'
docker compose down
```

## 六、开发标准流程（新接口 / 学校升级）

### A. 新增对外接口（checklist）

1. 路径挂在 `/api/v1/*`（管理端 `/api/v1/admin/*`）；nginx 模板按信任模型放对
   location（注入网关令牌 vs 管理端透传），并同步 `tests/test_frontend_csp.py` 的断言；
2. 成功响应用 `ok_envelope(data, trace_id=..., extra_meta=...)`；可预期失败抛
   `ApiError`（码先在 `CODES` 登记）；未知异常交给全局处理器；
3. 列表接口分页用 `pagination(total, offset, limit)`；统计进 `data.stats`；
4. 横切依赖（鉴权/限流）继续抛 HTTPException，由全局处理器归一，不要自行格式化错误体；
5. 测试进 `tests/test_api_semantics.py` 或领域测试文件：断言信封五键、`code`、
   真实状态码；禁止对 `message` 文案做断言（文案可自由改进，码不可变）；
6. 契约变更同步 `docs/api-semantics.md`（错误码表/迁移映射）。

### C. 学校侧升级应急（按 docs/upstream-contract.md 全文执行，此处为速查）

1. `/health/public` 的 `school.layers` 定位漂移层 →
   `python scripts/contract_selfcheck.py --json` 逐条判定契约事实；
2. 只改 `school_contract.py`（或路径类改 `school_endpoints.py`），递增
   `SCHOOL_CONTRACT_VERSION`，更新 `docs/upstream-contract.md` 第 2 节事实表；
3. 跑第五节全部验证命令（含反重复扫描与自检退出码 0）后部署。

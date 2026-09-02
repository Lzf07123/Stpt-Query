# edu-query-app 项目协作手册（多 Agents）

> 本文件是给后续 AI Agent（Codex 等）的「项目宪法」。**新会话必须先完整读完本文件与 README 再动手；子 agent 接单后同样先读本文件与第二节事实来源。** 人类开发者同样适用。

## 一、项目是什么

edu-query-app 把固定的「汕职院教务信息查询」Dify 工作流重写为三容器编排服务，不再依赖 Dify：

- **get-infomation-service（查询代理）**：源码自包含于本仓库同名目录，负责学校统一认证登录、免密跳转、成绩/课表原始查询。
- **format-service（格式化后端）**：本仓库维护，负责编排固定工作流、成绩/课表 Markdown 渲染、成绩分析 LLM、异常分类与 PDF。
- **frontend（前端）**：Nginx 托管原 dify-workflow-api 页面，是默认唯一对外入口，反向代理 `/run`、`/service-status`、`/health*` 并注入网关令牌。

## 二、事实来源

动手前先读以下文件，禁止用猜测代替调查：

- `README.md`：架构、契约与使用方式。
- `docker-compose.yml`：三容器、端口与网络的唯一事实（仅 frontend 暴露宿主端口）。
- `format-service/app/`：编排与渲染代码事实。
- `frontend/`：页面与 nginx 模板事实。
- `design-system/edu-query-app/`：项目内品牌方案（BRAND/MASTER），前端视觉决策的唯一事实来源。
- `tests/`：行为契约（渲染、分类、编排、HTTP、查询日志、安全与过载保护）。
- `docs/kubernetes-migration.md`：未来 K8s 迁移映射。
- `Li-Design/`：Git 子模块，**仅作设计/README 规范参考，非运行时依赖**。

## 三、硬性规则

1. **三容器边界不变**：查询代理源码自包含在 `get-infomation-service/`；禁止把查询代理再复制到其他服务目录，也不允许绕过本仓库恢复兄弟仓库构建依赖。
2. **默认唯一对外入口**：默认编排只有 `frontend` 映射宿主端口；`format-service` 与 `get-infomation-service` 不得在默认拓扑暴露宿主端口。开发/验收可显式加载 `compose.direct-*.yml` 直连，默认绑定宿主回环地址；公网直连必须先补 TLS、防火墙、访问控制与安全验收。查询代理默认仅挂 `internal` 网络。
3. **编排层无状态**：`format-service` 不落盘业务状态；PDF 以 Base64 内联返回，不引入文件存储/PV。
4. **令牌安全**：固定 `API_TOKEN`（前端与后端共用）；nginx 反代时注入 `Authorization`；页面不得持有令牌；日志与响应不得输出密码/session/token。
5. **单一事实来源**：提示词只在 `prompts.py`、异常规则只在 `classifier.py`、渲染逻辑只在 `render.py`、环境变量文档只在 `.env.example`，禁止重复实现。
6. **命名连字符**：服务、目录、镜像一律连字符（`format-service`、`frontend`、`get-infomation-service`）。
7. **完成 = 验证 + 文档**：声称完成前跑第五节验证命令并保留输出；涉及结构/契约变更时同步 README 与 `.env.example`。
8. **不做破坏性操作**：不执行 `rm -rf`、force push、删除他人提交；不动未提交的改动。

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

# 2. 单元测试（当前基线 125 个用例，全部通过）
docker build -q -t format-service:test format-service
docker run --rm -v "$PWD":/app -w /app format-service:test \
  sh -c "pip install -q pytest pytest-asyncio requests redis && python -m pytest -q"

# 2b. 前端令牌构建（改动 frontend/src/ 或 brand.js 后必须重编译）
cd frontend && npm install && npm run build && cd ..
# 改动 frontend/static/index.html 内联脚本后，必须同步更新
# frontend/templates/default.conf.template 中 script-src 对应内联脚本的
# SHA-256 哈希（否则 CSP 拦截脚本，查询页无法使用）。

# 3. 三容器端到端冒烟（需 Docker daemon）
API_TOKEN=smoke-token AUTO_ROTATE_TOKEN=false docker compose up -d --build
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/health/ready
curl -s -X POST http://127.0.0.1:8000/run \
  -H 'Content-Type: application/json' \
  -d '{"username":"2023000001","password":"pw","option":"成绩"}'
docker compose down
```

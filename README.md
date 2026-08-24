# edu-query-app · 教务查询编排服务

> 把固定的 Dify 工作流（汕职院教务信息查询）重写为独立 FastAPI 编排服务，
> 不再依赖 Dify。编排层无状态，复用现有 `get-infomation-service` 完成
> 登录 / 免密跳转 / 成绩 / 课表查询。

## 技术栈

| 层 | 选型 |
|---|---|
| 运行时 / Web | Python 3.11 · FastAPI + uvicorn |
| 上游调用 | httpx（异步） |
| 流程编排 | 纯 Python 顺序代码（`app/pipeline.py`），不引入工作流引擎 |
| LLM | OpenAI 兼容协议直连（默认 DeepSeek `deepseek-v4-flash`），可切换任意供应商 |
| 异常诊断 | 确定性规则分类器（`app/classifier.py`），替代原 4 个诊断 LLM |
| PDF | reportlab + 内置 CID 中文字体（`app/pdf.py`），替代 md_exporter 插件 |
| 部署 | Docker / Docker Compose（单/多实例）· Nginx · Redis（可选） |

## 与 Dify 工作流的映射

| Dify 节点 | 代码 |
|---|---|
| 开始节点 | `app/schema.py::WorkflowRequest` |
| HTTP 节点 ×4 | `app/pipeline.py` 调上游服务 |
| 代码节点 ×6 | `app/render.py`（1:1 移植） |
| 成绩分析 LLM | `app/llm.py` + `app/prompts.py` |
| 报错解析 LLM ×4 | `app/classifier.py` 确定性规则 |
| 变量聚合器 | 分支返回值（已消除登录响应泄漏） |
| md_exporter / file_tools | `app/pdf.py` + 内联 Base64 PDF |
| sys.workflow_run_id | `app/trace.py::new_run_id` |
| 3 个 END | `app/schema.py::QueryResult` 单一体 |

## 快速开始

```bash
cp .env.example .env   # 填写 SERVICE_API_TOKEN / LLM_API_KEY
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# 或：docker compose --profile single up --build
```

调用示例：

```bash
curl -X POST http://127.0.0.1:8000/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"username":"2023000001","password":"***","option":"成绩","check":false}'
```

响应：

```json
{"success": true, "kind": "grades", "output": "> 🔗 免密登录…", "run_id": "…", "meta": {}}
```

## 环境变量

见 `.env.example`。生产环境必须 `ENVIRONMENT=production`、`AUTO_ROTATE_TOKEN=false`
并配置固定 `API_TOKEN`（多副本一致）。

## 测试

```bash
pytest
```

## Kubernetes 迁移就绪

当前不随仓库附带 `k8s/` 清单，但代码与配置已按未来可迁入 K8s 设计：

- 编排层**无状态**、PDF 内联返回（无 PV 依赖），可水平扩容；
- 已提供 `GET /health/live`（存活）与 `GET /health/ready`（就绪）探针端点；
- 全部配置走环境变量（ConfigMap/Secret 可直接映射），生产强制固定 `API_TOKEN`；
- 多副本共享限流/状态历史可选开启 `REDIS_URL`。

详见 [docs/kubernetes-migration.md](docs/kubernetes-migration.md)。

## 路线图

- [ ] 多实例后台任务队列（异步 `/run/{id}` 轮询兼容）
- [ ] LLM 异常诊断兜底（规则未命中时）
- [ ] Prometheus 指标与告警
- [ ] 前端完整移植与 PDF 内联预览

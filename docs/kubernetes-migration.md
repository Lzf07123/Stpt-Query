# Kubernetes 迁移就绪说明

> 当前项目为小规模部署，使用 Docker Compose 单/多实例即可，不随仓库附带
> `k8s/` 清单。本节说明代码与配置已为未来迁入 Kubernetes 做好了哪些准备，
> 以及届时需要补充的内容。

## 一、已就绪的 K8s 友好设计

| 能力 | 现状 | K8s 对应 |
|---|---|---|
| 无状态编排层 | 业务状态在 `get-infomation-service`（Redis 共享）；本服务只落盘查询日志和全站通知配置降级文件。查询编排不落盘业务状态 | 可直接水平扩容；通知需共享 RWX PVC，其余副本可任意调度 |
| PDF 内联返回 | `pdf_base64` 直接放在响应里，无文件存储 | **无需 PV/PVC** |
| 环境变量配置 | 全部配置来自 `.env`/环境变量（`Settings`） | ConfigMap + Secret 直接映射 |
| 固定 API Token | 生产强制 `AUTO_ROTATE_TOKEN=false` + 固定 `API_TOKEN` | Secret 注入，多副本一致 |
| 管理员后台查询 | `ADMIN_TOKEN` 独立鉴权 `/admin/api/query-logs`、`/admin/api/metrics` | Secret 注入；多副本需统一 Ingress 或逐副本查看 |
| 存活/就绪探针 | `GET /health/live`、`GET /health/ready` | `livenessProbe` / `readinessProbe` |
| 优雅退出 | 资源采样任务由应用 shutdown 取消 | 配合 `terminationGracePeriodSeconds` |
| 异步任务队列 | `POST /run/jobs` + Redis 队列/状态/阶段；每副本 `JOB_WORKERS` 消费 | Redis 中的队列仅保存短期 session 和粗粒度阶段，不保存密码 |
| 客户端 IP | `TRUST_PROXY=true` 后按 `X-Forwarded-For` 限流 | Ingress/Service 传递真实 IP |
| 跨副本限流/状态历史 | format-service 用 `REDIS_URL` 共享固定窗口限流、历史与日志；查询代理用 `JWXT_REDIS_URL` 共享会话与缓存 | 接入托管 Redis（云 Redis / Operator） |
| 外部依赖降级 | Redis 故障时切本地限流/历史/会话；LLM 故障返回纯表；查询代理故障返回分类错误；后台展示降级提示 | 可避免单个外部依赖导致入口整体不可用 |
| 查询日志与服务状态 | 结构化 JSON 单行输出 stdout + 每副本 WAL + 进程内历史 + Redis（`gw:v2:query-logs`）+ `GET /query-logs`、`GET /service-status` | 服务状态由查询日志派生；采集 stdout 到集中日志；需要本地留存时只给每个 Pod 挂独立 PVC |
| 全站通知 | 共享 JSONL 文件为权威存储，Redis 为恢复副本；`GET /notices/active`、`GET /notices/history` | 多副本挂同一 `ReadWriteMany` PVC；托管存储不支持 RWX 时改为支持 `flock` 的网络文件系统或外部配置存储 |
| 镜像仓库前缀 | Compose 已支持 `IMAGE_REGISTRY` 拼接 | 推送到私有仓库后复用同一镜像名 |

## 二、迁入 K8s 时的映射清单（三容器 → 三个 Deployment）

| 现状（Compose） | K8s 对象 |
|---|---|
| `get-infomation-service` | Deployment + Service（有状态：会话/缓存，多副本需 Redis） |
| `format-service` | Deployment（查询编排无状态，`replicas>=2`）+ Service + 通知共享 RWX PVC |
| `frontend` | Deployment + Service + Ingress（终止 HTTPS） |
| `API_TOKEN` / `LLM_API_KEY` / `SERVICE_API_TOKEN` | Secret |
| `SERVICE_BASE_URL` / `LLM_*` / `RATE_LIMIT` | ConfigMap / 环境变量 |
| 内置 redis（后续多副本） | 托管 Redis 或 StatefulSet + PV（生产建议托管，避免会话丢失） |
| healthcheck | livenessProbe `/health/live`、readinessProbe `/health/ready` |

> 无状态要求：`format-service` 与 `frontend` 可任意扩容；`get-infomation-service`
> 单实例即可，多副本时为其配置 `JWXT_REDIS_URL` 共享状态。Redis 故障时查询代理会
> 降级为本副本内存状态，健康状态变为 `degraded`，但容器不应退出。

## 三、迁入前需要补充的事项（届时再做）

1. **镜像治理**：三个镜像统一语义化 tag + digest，开启镜像扫描与签名。
2. **资源规格**：`requests/limits`（CPU/内存）、`securityContext.runAsNonRoot`、
  `readOnlyRootFilesystem`（本服务查询编排无写盘需求；通知挂专用 RWX PVC）。
3. **探针参数**：liveness 建议 `initialDelaySeconds: 10, periodSeconds: 10`；
   readiness 建议 `periodSeconds: 10`，开启 `TRUST_PROXY=true` 后限流依赖 Ingress
   正确传递 `X-Forwarded-For`。
4. **优雅退出**：`terminationGracePeriodSeconds: 30`（uvicorn 默认在 SIGTERM
   后完成在途请求）。
5. **Redis 高可用**：多副本 + 限流/历史共享时用托管 Redis，避免单点。
6. **可观测**：查询日志已按结构化 JSON 单行输出 stdout（`event/run_id/username/option/success/kind/elapsed_ms`，无密码/session/token），迁入 K8s 后由 Fluentd / Promtail 采集；再接入 Prometheus `/metrics` 指标。
7. **密钥轮换**：`API_TOKEN`、`LLM_API_KEY`、`SERVICE_API_TOKEN` 按周期轮换。
8. **过载保护**：format-service 已有全局并发槽位、异步任务队列、LLM/PDF 独立槽位、槽位等待超时和单次编排总预算；副本数仍需结合 `GLOBAL_CONCURRENCY`、`JOB_WORKERS` 与查询代理容量评估。

## 四、示例（仅供未来参考，不在当前仓库落地）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: format-service
spec:
  replicas: 2
  selector:
    matchLabels: { app: format-service }
  template:
    metadata:
      labels: { app: format-service }
    spec:
      terminationGracePeriodSeconds: 30
      containers:
        - name: app
          image: registry.example.com/edu-query-format:0.1.0
          ports: [{ containerPort: 8000 }]
          env:
            - name: ENVIRONMENT
              value: "production"
            - name: SERVICE_BASE_URL
              value: "http://get-infomation-service:8766"
            - name: LLM_BASE_URL
              value: "https://open.bigmodel.cn/api/paas/v4"
            - name: LLM_MODEL
              value: "glm-4.5-flash"
            - name: LLM_ENABLE_THINKING
              value: "true"
            - name: LLM_SYSTEM_PROMPT
              value: ""
            - name: TRUST_PROXY
              value: "true"
          livenessProbe:
            httpGet: { path: /health/live, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            httpGet: { path: /health/ready, port: 8000 }
            periodSeconds: 10
          resources:
            requests: { cpu: 100m, memory: 128Mi }
            limits: { cpu: 500m, memory: 512Mi }
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
```

> 正式迁移时再补充 Service/Ingress/Secret/PDB 与托管 Redis 配置，按上述映射落地即可。

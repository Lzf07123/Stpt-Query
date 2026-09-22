# Kubernetes 迁移就绪说明

> 当前项目为小规模部署，使用 Docker Compose 单实例即可，不随仓库附带
> `k8s/` 清单。本节说明代码与配置已为未来迁入 Kubernetes 做好了哪些准备，
> 以及届时需要补充的内容。

## 一、已就绪的 K8s 友好设计

| 能力 | 现状 | K8s 对应 |
|---|---|---|
| 单一应用 | 编排、渲染、PDF 与内嵌查询代理同进程（`app/`），无跨服务 HTTP 调用 | 一个 Deployment + Service |
| 业务状态 | 查询会话/缓存/限流在进程内存；持久状态仅两项：查询日志文件、全站通知 JSONL（日历订阅落本地库，见第六节） | 单实例部署；持久卷挂载 |
| PDF 内联返回 | `pdf_base64` 直接放在响应里，无文件存储 | **无需 PV/PVC** |
| 环境变量配置 | 全部配置来自 `.env`/环境变量（`Settings`） | ConfigMap + Secret 直接映射 |
| 固定 API Token | 生产强制 `AUTO_ROTATE_TOKEN=false` + 固定 `API_TOKEN` | Secret 注入 |
| 管理员后台查询 | `ADMIN_TOKEN` 独立鉴权 `/admin/api/*` | Secret 注入 |
| 存活/就绪探针 | `GET /health/live`、`GET /health/ready`（ready 会探测内嵌查询代理） | `livenessProbe` / `readinessProbe` |
| 优雅退出 | 采样任务与内嵌查询代理后台线程由应用 shutdown 取消 | 配合 `terminationGracePeriodSeconds` |
| 异步任务队列 | `POST /run/jobs` 需要 `REDIS_URL`；未配置时返回 503，前端回退同步查询 | 需要排队时接托管 Redis |
| 客户端 IP | `TRUST_PROXY=true` 后按 `X-Forwarded-For` 限流 | Ingress/Service 传递真实 IP |
| 外部依赖降级 | 无 Redis 时使用进程内限流/历史/会话；LLM 故障返回纯表；学校不可达返回分类错误 | 单个外部依赖故障不拖垮入口 |
| 查询日志与服务状态 | 结构化 JSON 单行输出 stdout + 本地 JSONL + 进程内历史；`GET /query-logs`、`GET /service-status` | 采集 stdout；需要本地留存时给 Pod 挂独立 PVC |
| 全站通知 | JSONL 文件为权威存储（可选 Redis 恢复副本） | 单实例用独立 PVC；多副本需 `ReadWriteMany` |
| 镜像仓库前缀 | Compose 已支持 `IMAGE_REGISTRY` 拼接 | 推送到私有仓库后复用同一镜像名 |

## 二、迁入 K8s 时的映射清单（两容器 → 两个 Deployment）

| 现状（Compose） | K8s 对象 |
|---|---|
| `app` | Deployment（默认 `replicas: 1`）+ Service + 查询日志/通知 PVC |
| `frontend` | Deployment + Service + Ingress（终止 HTTPS） |
| `API_TOKEN` / `ADMIN_TOKEN` / `LLM_API_KEY` | Secret |
| `LLM_*` / `RATE_LIMIT` / `JWXT_*` | ConfigMap / 环境变量 |
| 可选外部 Redis | 托管 Redis（仅在需要异步任务或多副本时接入） |

> **为什么默认单副本**：查询会话、限流桶、短码都在进程内存中，内嵌查询代理因此与
> 副本绑定。要水平扩容必须同时接入托管 Redis（`REDIS_URL` + `JWXT_REDIS_URL`），
> 否则同一账号的会话会在副本间漂移。

## 三、迁入前需要补充的事项（届时再做）

1. **镜像治理**：两个镜像统一语义化 tag + digest，开启镜像扫描与签名。
2. **资源规格**：`requests/limits`（CPU/内存，含 LibreOffice 转换余量）、
   `securityContext.runAsNonRoot`、`readOnlyRootFilesystem`（写盘仅限挂载的卷与 `/tmp`）。
3. **探针参数**：liveness 建议 `initialDelaySeconds: 10, periodSeconds: 10`；
   readiness 建议 `periodSeconds: 10`；`TRUST_PROXY=true` 时限流依赖 Ingress 正确传递 `X-Forwarded-For`。
4. **优雅退出**：`terminationGracePeriodSeconds: 30`（uvicorn 默认在 SIGTERM 后完成在途请求）。
5. **可选 Redis**：需要异步队列或多副本时接入托管 Redis，避免单点。
6. **可观测**：查询日志已按结构化 JSON 单行输出 stdout（无密码/session/token），迁入后由
   Fluentd / Promtail 采集；再接入 Prometheus `/metrics` 指标（边缘已屏蔽该路径）。
7. **密钥轮换**：`API_TOKEN`、`ADMIN_TOKEN`、`LLM_API_KEY` 按周期轮换。
8. **过载保护**：已有全局并发槽位、LLM/PDF 独立槽位、槽位等待超时与单次编排总预算；
   副本数仍需结合 `GLOBAL_CONCURRENCY` 与内嵌查询代理容量评估。

## 四、示例（仅供未来参考，不在当前仓库落地）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: edu-query-app
spec:
  replicas: 1
  selector:
    matchLabels: { app: edu-query-app }
  template:
    metadata:
      labels: { app: edu-query-app }
    spec:
      terminationGracePeriodSeconds: 30
      containers:
        - name: app
          image: registry.example.com/edu-query-app:1.0.0
          ports: [{ containerPort: 8000 }]
          env:
            - name: ENVIRONMENT
              value: "production"
            - name: TRUST_PROXY
              value: "true"
            - name: PUBLIC_BASE_URL
              value: "https://example.com"
            - name: ADMIN_TOKEN
              valueFrom: { secretKeyRef: { name: edu-query-secrets, key: admin-token } }
          volumeMounts:
            - { name: query-logs, mountPath: /var/log/edu-query }
            - { name: notices, mountPath: /var/lib/edu-query/notices }
            - { name: tmp, mountPath: /tmp }
          livenessProbe:
            httpGet: { path: /health/live, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            httpGet: { path: /health/ready, port: 8000 }
            periodSeconds: 10
          resources:
            requests: { cpu: 200m, memory: 384Mi }
            limits: { cpu: "1", memory: 640Mi }
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
      volumes:
        - { name: query-logs, persistentVolumeClaim: { claimName: edu-query-logs } }
        - { name: notices, persistentVolumeClaim: { claimName: edu-query-notices } }
        - { name: tmp, emptyDir: { sizeLimit: 256Mi } }
```

## 五、Image Pull 与滚动更新注意

- LibreOffice 层较大，建议节点预热镜像或使用私有仓库就近拉取。
- 单副本部署滚动更新期间会有秒级不可用；需要零中断时先接入 Redis 并扩容到 2 副本。

## 六、日历订阅（Phase B）的额外要求

- 订阅数据落在本地库（SQLite），与内嵌查询代理共享单实例语义；迁入 K8s 时必须挂
  持久卷，且**保持单副本**，或改为外部数据库 + 独立迁移改造。
- 托管凭据使用 `CALENDAR_MASTER_KEY`（Secret 注入，禁止自动生成），备份中不得与
  数据库同一份介质保存。

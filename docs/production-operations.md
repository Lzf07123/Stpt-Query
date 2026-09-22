# Production Operations

## Capacity

The school-side safety boundary is the hard limit. The default topology is a
**single app instance** (monolith with an embedded query proxy), so keep:

```text
GLOBAL_CONCURRENCY <= JWXT_UPSTREAM_GLOBAL
```

If you later scale out (requires an external Redis for `REDIS_URL` and
`JWXT_REDIS_URL`), the bound becomes `replicas * GLOBAL_CONCURRENCY <= JWXT_UPSTREAM_GLOBAL`.

| Parameter | Default | Production baseline |
| --- | ---: | ---: |
| `GLOBAL_CONCURRENCY` | 4 | 4 (single instance) |
| `JWXT_UPSTREAM_GLOBAL` | 8 | 8 |
| `RATE_LIMIT` | 30/min/IP | Start at 30; raise only after real-traffic metrics |
| `LLM_CONCURRENCY` | 8 | 4 unless the provider quota is higher |
| `PDF_CONCURRENCY` | 2 | 2 on a 1 vCPU-class host |
| `JWXT_PDF_CONCURRENCY` | 1 | 1 unless the host has spare CPU and memory |
| `JWXT_PDF_TIMEOUT` | 30s | 30s; raise only after observing conversion metrics |
| `JOB_WORKERS` | 2 | Only used when `REDIS_URL` is set |
| `JWXT_LOGIN_RATE_LIMIT` | 60/min | Lower if the school reports lockouts |

Do not raise `GLOBAL_CONCURRENCY` and `JWXT_UPSTREAM_GLOBAL` independently. Use
request duration, LLM failures, concurrency wait time, and upstream metrics to
calibrate.

## Topology

| Component | Role | Notes |
| --- | --- | --- |
| `frontend` | Only host-exposed container (`APP_PORT`) | Static pages, CSP headers, injects `API_TOKEN` for the app |
| `app` | Monolith: orchestration + rendering + LLM analysis + PDF + embedded query proxy (in-process ASGI) | Single instance by default; no internal network or service token |
| Redis | **Optional**, external only | Enables `/run/jobs` (async queue) and cross-replica state; not shipped in Compose |

When Redis is absent, `/run/jobs` returns 503 and the page falls back to the
synchronous `POST /run` path. Rate limiting, query history, and query sessions
stay in process memory.

## Retention And Backup

| Data | Location | Retention | Backup |
| --- | --- | --- | ---: |
| Query JSONL | `app-query-logs` named volume | 7 files, 50 MiB each (`FILE_LOG_*`) | Sync stdout/JSONL to the centralized log platform; optionally archive the volume |
| Notice JSONL | `app-notice-fallback` named volume | `NOTICE_HISTORY_MAX` (500) with compaction | Copy the volume or the JSONL file daily |
| Optional Redis | External instance | Follow TTLs; query history is capped | Run `scripts/backup-redis.sh` daily (14-day default) when Redis is used |
| Service stdout | Docker logging driver | Configure the host logging driver | Ship to centralized logs; do not store credentials |
| Built images | Local Docker/registry | Prune superseded tags after a release | Keep release tags, not a rolling `latest` tag |

The backup script performs an RDB `SAVE`, copies `dump.rdb` out of the
container, compresses it, and removes backups older than
`REDIS_BACKUP_RETENTION_DAYS`. It only applies to an external/self-hosted Redis;
there is no Redis container in the default topology.

## Launch Checklist

1. Set `ENVIRONMENT=production`, `AUTO_ROTATE_TOKEN=false`, and strong
   `API_TOKEN` / `ADMIN_TOKEN` only in the deployment `.env`.
2. Set the HTTPS `PUBLIC_BASE_URL`; verify certificate renewal, HTTP redirect,
   and HSTS.
3. Keep `JWXT_ALLOW_GET_CREDENTIALS=0`, `JWXT_PROTECT_LOGIN_STATUS=1`, and
   `JWXT_VERIFY_TLS=1`.
4. Leave `REDIS_URL` / `JWXT_REDIS_URL` empty unless you intentionally enable
   async jobs or multiple replicas with an external Redis.
5. Confirm that only `frontend` publishes a host port (`docker compose ps`).
6. Run Compose validation, the container pytest suite, frontend build,
   dependency audit, and the image build.
7. Smoke test home, ready health, service status, grades, schedule, PDF, and
   jump-link flows.
8. Scan stdout, query logs, and (when present) Redis samples for password,
   session, token, and authorization leakage.

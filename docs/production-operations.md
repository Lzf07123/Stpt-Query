# Production Operations

## Capacity

The school-side safety boundary is the hard limit. With the default two
format-service replicas, keep this relation true:

```text
FORMAT_REPLICAS * GLOBAL_CONCURRENCY <= JWXT_UPSTREAM_GLOBAL
```

| Parameter | Default | Production baseline |
| --- | ---: | ---: |
| `FORMAT_REPLICAS` | 2 | 2 |
| `GLOBAL_CONCURRENCY` | 4 | 4 per replica (8 total) |
| `JWXT_UPSTREAM_GLOBAL` | 8 | 8 |
| `RATE_LIMIT` | 30/min/IP | Start at 30; raise only after real-traffic metrics |
| `LLM_CONCURRENCY` | 8 | 4 per replica unless the provider quota is higher |
| `PDF_CONCURRENCY` | 2 | 2 per replica on a 1 vCPU-class host |
| `JOB_WORKERS` | 2 | 2 per replica; active jobs are bounded by `GLOBAL_CONCURRENCY` |
| `JWXT_PDF_CONCURRENCY` | 1 | 1 unless the host has spare CPU and memory |
| `JWXT_PDF_TIMEOUT` | 30s | 30s; raise only after observing conversion metrics |

Do not raise `JOB_WORKERS`, `GLOBAL_CONCURRENCY`, and `JWXT_UPSTREAM_GLOBAL`
independently. Use Prometheus request duration, LLM failures, concurrency wait
time, and query-proxy upstream metrics to calibrate.

## Retention And Backup

| Data | Location | Retention | Backup |
| --- | --- | --- | ---: |
| Query JSONL | `format-query-logs` named volume | 7 files, 50 MiB each (`FILE_LOG_*`) | Sync stdout/JSONL to the centralized log platform; optionally archive the volume |
| Redis business state | `redis-data` named volume | Follow TTLs; query history is capped | Run `scripts/backup-redis.sh` daily (14-day default) |
| Service stdout | Docker logging driver | Configure the host logging driver | Ship to centralized logs; do not store credentials |
| Built images | Local Docker/registry | Prune superseded tags after a release | Keep release tags, not a rolling `latest` tag |

The built-in Redis enables AOF with `everysec`. The backup script performs an
RDB `SAVE`, copies `dump.rdb` out of the container, compresses it, and removes
backups older than `REDIS_BACKUP_RETENTION_DAYS`. Schedule it outside peak query
hours and copy the resulting file to a second storage location.

## Launch Checklist

1. Set `ENVIRONMENT=production`, `AUTO_ROTATE_TOKEN=false`, and all three strong
   tokens only in the deployment `.env`.
2. Set the HTTPS `PUBLIC_BASE_URL`; verify certificate renewal, HTTP redirect,
   and HSTS.
3. Keep `JWXT_ALLOW_GET_CREDENTIALS=0`, `JWXT_PROTECT_LOGIN_STATUS=1`, and
   `JWXT_VERIFY_TLS=1`.
4. Use `COMPOSE_PROFILES=redis`, database `0` for format-service and database
   `1` for the query proxy, and verify AOF.
5. Confirm that only frontend publishes a host port.
6. Run Compose validation, the container pytest suite, frontend build, dependency
   audit, and image build.
7. Smoke test home, ready health, service status, grades, schedule, PDF, and
   jump-link flows.
8. Scan stdout, query logs, and Redis samples for password, session, token, and
   authorization leakage.

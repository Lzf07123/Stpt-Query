"""容器与宿主机资源采样：读取 Linux cgroup 与聚合 /proc 指标。"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional


_CGROUP_ROOT = Path(os.environ.get("CGROUP_ROOT", "/sys/fs/cgroup"))
_PROC_ROOT = Path(os.environ.get("HOST_PROC_ROOT", "/proc"))
_HOST_CGROUP_ROOT = Path(os.environ.get("HOST_CGROUP_ROOT", "/host/sys/fs/cgroup"))
_HOST_PROC_CONFIGURED = _PROC_ROOT != Path("/proc")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_int(path: Path) -> Optional[int]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _cgroup_memory() -> tuple[Optional[int], Optional[int]]:
    current = _read_int(_CGROUP_ROOT / "memory.current")
    limit = _read_int(_CGROUP_ROOT / "memory.max")
    if current is None:
        current = _read_int(_CGROUP_ROOT / "memory/memory.usage_in_bytes")
        limit_v1 = _read_int(_CGROUP_ROOT / "memory/memory.limit_in_bytes")
        if limit is None and limit_v1 is not None and limit_v1 < (1 << 60):
            limit = limit_v1
    return current, limit


def _proc_root() -> Path:
    """优先使用宿主机 proc 挂载；未挂载时降级为当前内核视图。"""
    try:
        if (_PROC_ROOT / "stat").is_file():
            return _PROC_ROOT
    except OSError:
        pass
    return Path("/proc")


def _read_process_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _process_cmdline(process_path: Path) -> str:
    return _read_process_text(process_path / "cmdline").replace("\x00", " ").strip()


def _process_cgroup_id(process_path: Path) -> Optional[str]:
    for line in _read_process_text(process_path / "cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        candidate = fields[2].rstrip("/").rpartition("/")[2]
        if _CONTAINER_ID_RE.fullmatch(candidate):
            return candidate
    return None


def _process_rss_bytes(process_path: Path) -> Optional[int]:
    for line in _read_process_text(process_path / "status").splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                return None
    return None


def _host_cgroup_values(cgroup_id: str) -> tuple[Optional[int], Optional[int]]:
    root = _HOST_CGROUP_ROOT / "docker" / cgroup_id
    current = _read_int(root / "memory.current")
    limit = _read_int(root / "memory.max")
    if limit is not None and limit >= (1 << 60):
        limit = None
    return current, limit


def _classify_orchestration_process(command: str) -> Optional[str]:
    normalized = " ".join(command.split())
    if "uvicorn" in normalized and "app.main:app" in normalized:
        return "format-service"
    if normalized.startswith("python") and "main.py" in normalized:
        return "get-infomation-service"
    if normalized.startswith("nginx:") and "master process" in normalized:
        return "frontend"
    if normalized.startswith("redis-server"):
        return "redis"
    return None


def _orchestration_memory() -> dict:
    service_processes: dict[str, list[dict]] = {
        "format-service": [],
        "get-infomation-service": [],
        "frontend": [],
        "redis": [],
    }
    proc_root = _proc_root()
    try:
        process_ids = [item.name for item in proc_root.iterdir() if item.name.isdigit()]
    except OSError:
        process_ids = []

    for pid in process_ids:
        service = _classify_orchestration_process(_process_cmdline(proc_root / pid))
        if service is None:
            continue
        cgroup_id = _process_cgroup_id(proc_root / pid)
        rss_bytes = _process_rss_bytes(proc_root / pid)
        service_processes[service].append({
            "pid": int(pid), "cgroup_id": cgroup_id, "rss_bytes": rss_bytes,
        })

    services: dict[str, dict] = {}
    total_bytes = 0
    total_limit: Optional[int] = 0
    cgroup_count = 0
    for service, processes in service_processes.items():
        memory_bytes: Optional[int] = None
        limit_bytes: Optional[int] = None
        source = "unavailable"
        if service == "format-service":
            current, limit = _cgroup_memory()
            if current is not None:
                memory_bytes, limit_bytes, source = current, limit, "cgroup"
                cgroup_count += 1
        else:
            cgroup_id = next((item["cgroup_id"] for item in processes if item["cgroup_id"]), None)
            if cgroup_id:
                current, limit = _host_cgroup_values(cgroup_id)
                if current is not None:
                    memory_bytes, limit_bytes, source = current, limit, "cgroup"
                    cgroup_count += 1
        if memory_bytes is None and processes:
            rss_values = [item["rss_bytes"] for item in processes if item["rss_bytes"] is not None]
            if rss_values:
                memory_bytes = sum(rss_values)
                source = "process_rss"
        if memory_bytes is not None:
            total_bytes += memory_bytes
        if limit_bytes is None:
            total_limit = None
        elif total_limit is not None:
            total_limit += limit_bytes
        services[service] = {
            "memory_bytes": memory_bytes,
            "limit_bytes": limit_bytes,
            "process_count": len(processes),
            "source": source,
        }

    discovered = sum(1 for item in services.values() if item["memory_bytes"] is not None)
    return {
        "memory_bytes": total_bytes if discovered else None,
        "limit_bytes": total_limit,
        "source": "cgroup" if cgroup_count else ("process_rss" if discovered else "unavailable"),
        "discovered_services": discovered,
        "expected_services": len(services),
        "services": services,
    }


def _host_cpu_times() -> Optional[tuple[int, int]]:
    try:
        for line in (_proc_root() / "stat").read_text(encoding="utf-8").splitlines():
            if not line.startswith("cpu "):
                continue
            values = [int(value) for value in line.split()[1:9]]
            idle = values[3] + values[4]
            return sum(values), idle
    except (OSError, ValueError, IndexError):
        pass
    return None


def _host_memory() -> dict:
    values: dict[str, int] = {}
    try:
        for line in (_proc_root() / "meminfo").read_text(encoding="utf-8").splitlines():
            key, remainder = line.split(":", 1)
            fields = remainder.split()
            if fields:
                values[key.strip()] = int(fields[0]) * 1024
    except (OSError, ValueError):
        return {}

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
    used = max(0, total - available) if total is not None else None
    percent = round(used / total * 100, 1) if total and used is not None else None
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available if total is not None else None,
        "percent": percent,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": max(0, swap_total - swap_free),
    }


def _host_load() -> dict:
    try:
        fields = (_proc_root() / "loadavg").read_text(encoding="utf-8").split()
        return {"one_minute": float(fields[0]), "five_minutes": float(fields[1]),
                "fifteen_minutes": float(fields[2])}
    except (OSError, ValueError, IndexError):
        return {"one_minute": None, "five_minutes": None, "fifteen_minutes": None}


def _host_cpu_count() -> Optional[int]:
    try:
        return int(os.cpu_count() or 0) or None
    except Exception:
        return None


def _host_disk_activity() -> Optional[dict]:
    totals = {
        "reads_completed": 0, "writes_completed": 0,
        "sectors_read": 0, "sectors_written": 0,
    }
    found = False
    try:
        for line in (_proc_root() / "diskstats").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 14:
                continue
            device = fields[2]
            if device.startswith(("loop", "ram", "fd", "sr")):
                continue
            found = True
            totals["reads_completed"] += int(fields[3])
            totals["sectors_read"] += int(fields[5])
            totals["writes_completed"] += int(fields[7])
            totals["sectors_written"] += int(fields[9])
    except (OSError, ValueError, IndexError):
        return None
    return totals if found else None


def _host_uptime_seconds() -> Optional[int]:
    try:
        return int(float((_proc_root() / "uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _host_network_totals() -> dict[str, int]:
    totals = {"received_bytes": 0, "sent_bytes": 0}
    try:
        for line in (_proc_root() / "net" / "dev").read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            name, columns_text = line.split(":", 1)
            if name.strip() == "lo":
                continue
            columns = columns_text.split()
            totals["received_bytes"] += int(columns[0])
            totals["sent_bytes"] += int(columns[8])
    except (OSError, IndexError, ValueError):
        pass
    return totals


def _cpu_stats() -> tuple[int, int]:
    try:
        lines = (_CGROUP_ROOT / "cpu.stat").read_text(encoding="utf-8").splitlines()
        values = dict(line.split(maxsplit=1) for line in lines)
        return int(values["usage_usec"]), int(values["nr_throttled"])
    except (OSError, KeyError, TypeError):
        pass
    total = 0
    try:
        for stat_path in sorted((_CGROUP_ROOT / "cpuacct").glob("cpuacct.usage")):
            total += int(stat_path.read_text().strip())
    except (OSError, ValueError):
        pass
    return total // 1000, 0


def _network_totals() -> dict[str, int]:
    totals = {"received_bytes": 0, "sent_bytes": 0}
    try:
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            name, columns_text = line.split(":", 1)
            if name.strip() == "lo":
                continue
            columns = columns_text.split()
            totals["received_bytes"] += int(columns[0])
            totals["sent_bytes"] += int(columns[8])
    except (OSError, IndexError, ValueError):
        pass
    return totals


def _rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        pass
    try:
        return len(Path(f"/proc/{os.getpid()}/maps").read_bytes())
    except OSError:
        return 0


class ResourceMonitor:
    """周期采集并保留近三分钟资源样本。"""

    def __init__(self, log_path: str = "", interval: float = 2.0,
                 history_size: int = 150) -> None:
        self.log_path = log_path
        self.interval = max(0.5, interval)
        self.samples: deque[dict] = deque(maxlen=max(10, history_size))
        self._lock = Lock()
        self._started_at = time.time()
        self._last_cpu: Optional[tuple[float, float]] = None
        self._last_host_cpu: Optional[tuple[float, int, int]] = None
        self._last_disk_activity: Optional[tuple[float, dict]] = None
        self._last_host_network: Optional[tuple[float, dict]] = None
        self._last_log_size: Optional[tuple[float, int]] = None
        self._task: Optional[asyncio.Task] = None

    def collect(self) -> dict:
        now = time.monotonic()
        cpu_usec, throttled_count = _cpu_stats()
        cpu_seconds = cpu_usec / 1_000_000
        cpu_percent: Optional[float] = None
        if self._last_cpu is not None:
            previous_time, previous_seconds = self._last_cpu
            elapsed = now - previous_time
            used = cpu_seconds - previous_seconds
            if elapsed >= 0.25:
                cpu_percent = round(max(0.0, min(100.0, used / elapsed * 100)), 1)
        self._last_cpu = (now, cpu_seconds)

        memory_used, memory_limit = _cgroup_memory()
        memory_total = memory_limit
        memory_percent: Optional[float] = None
        if memory_used is not None and memory_total is not None and memory_total > 0:
            memory_percent = round(memory_used / memory_total * 100, 1)

        rss_bytes = _rss_bytes()
        disk = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
        try:
            usage = shutil.disk_usage(self.log_path or "/tmp")
            disk.update(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)
        except (OSError, ValueError):
            pass

        host_cpu_times = _host_cpu_times()
        host_cpu_percent: Optional[float] = None
        if host_cpu_times is not None and self._last_host_cpu is not None:
            previous_at, previous_total, previous_idle = self._last_host_cpu
            elapsed = now - previous_at
            total_delta = host_cpu_times[0] - previous_total
            idle_delta = host_cpu_times[1] - previous_idle
            if elapsed >= 0.25 and total_delta > 0:
                host_cpu_percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)
        if host_cpu_times is not None:
            self._last_host_cpu = (now, host_cpu_times[0], host_cpu_times[1])

        host_disk_activity = _host_disk_activity()
        host_disk_rates = {
            "read_iops": None, "write_iops": None,
            "read_bytes_per_second": None, "write_bytes_per_second": None,
        }
        if host_disk_activity is not None and self._last_disk_activity is not None:
            previous_at, previous = self._last_disk_activity
            elapsed = max(0.001, now - previous_at)
            host_disk_rates = {
                "read_iops": round(max(0, host_disk_activity["reads_completed"] - previous["reads_completed"]) / elapsed, 1),
                "write_iops": round(max(0, host_disk_activity["writes_completed"] - previous["writes_completed"]) / elapsed, 1),
                "read_bytes_per_second": round(max(0, host_disk_activity["sectors_read"] - previous["sectors_read"]) * 512 / elapsed, 1),
                "write_bytes_per_second": round(max(0, host_disk_activity["sectors_written"] - previous["sectors_written"]) * 512 / elapsed, 1),
            }
        if host_disk_activity is not None:
            self._last_disk_activity = (now, host_disk_activity)

        host_network = _host_network_totals()
        host_network_rates = {"received_bytes_per_second": None, "sent_bytes_per_second": None}
        if self._last_host_network is not None:
            previous_at, previous = self._last_host_network
            elapsed = max(0.001, now - previous_at)
            host_network_rates = {
                "received_bytes_per_second": round(max(0, host_network["received_bytes"] - previous["received_bytes"]) / elapsed, 1),
                "sent_bytes_per_second": round(max(0, host_network["sent_bytes"] - previous["sent_bytes"]) / elapsed, 1),
            }
        self._last_host_network = (now, host_network)

        try:
            log_path = Path(self.log_path) if self.log_path else None
            log_size = log_path.stat().st_size if log_path and log_path.exists() else None
        except OSError:
            log_size = None
        log_growth: Optional[float] = None
        if log_size is not None and self._last_log_size is not None:
            previous_at, previous_size = self._last_log_size
            if log_size >= previous_size:
                log_growth = round((log_size - previous_size) / max(0.001, now - previous_at), 1)
        if log_size is not None:
            self._last_log_size = (now, log_size)
        backup_count = 0
        if log_path and self.log_path:
            try:
                backup_count = len(list(log_path.parent.glob(log_path.name + ".*")))
            except OSError:
                backup_count = 0

        uptime = time.time() - self._started_at
        host_proc_available = _HOST_PROC_CONFIGURED and (_PROC_ROOT / "stat").is_file()
        sample = {
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "uptime_seconds": int(uptime),
            "cpu_percent": cpu_percent,
            "cpu_usage_seconds": round(cpu_seconds, 3),
            "cpu_throttled": throttled_count,
            "memory": {
                "used_bytes": memory_used,
                "limit_bytes": memory_limit,
                "percent": memory_percent,
                "limit_source": "cgroup",
            },
            "process": {
                "pid": os.getpid(),
                "rss_bytes": rss_bytes,
            },
            "disk": {
                "path": self.log_path or "/tmp",
                **disk,
            },
            "network": _network_totals(),
            "orchestration": {
                "memory": _orchestration_memory(),
            },
            "host": {
                "source": "host_proc" if host_proc_available else "container_kernel",
                "scope": "docker_vm" if os.path.exists("/.dockerenv") else "linux_kernel",
                "cpu_percent": host_cpu_percent,
                "cpu_count": _host_cpu_count(),
                "memory": _host_memory(),
                "load": _host_load(),
                "uptime_seconds": _host_uptime_seconds(),
                "disk": {
                    **host_disk_rates,
                    "reads_completed": host_disk_activity.get("reads_completed") if host_disk_activity else None,
                    "writes_completed": host_disk_activity.get("writes_completed") if host_disk_activity else None,
                },
                "network": {
                    **host_network,
                    **host_network_rates,
                },
            },
            "storage": {
                "available": log_size is not None,
                "file_bytes": log_size,
                "file_growth_bytes_per_second": log_growth,
                "backup_count": backup_count,
                "disk": disk,
            },
        }
        with self._lock:
            self.samples.append(sample)
        return dict(sample)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self.collect)
            await asyncio.sleep(self.interval)

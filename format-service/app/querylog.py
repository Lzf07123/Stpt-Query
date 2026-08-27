"""查询日志：输出结构化 JSON 单行日志，并提供可选 JSONL 文件通道。"""
from __future__ import annotations

import json
import logging
import os
from threading import Lock
from typing import Any, Dict, Iterator

LOG = logging.getLogger("edu-query-app.query")

# 结构性白名单之外的键即使误传也不会进入日志
_ALLOWED_FIELDS = {
    "event", "time", "run_id", "client_ip", "username", "option",
    "semesters", "weeks", "md2pdf", "check", "success", "kind",
    "elapsed_ms", "message", "analysis", "analysis_usage",
    "response_summary",
}

_SENSITIVE_FIELDS = {"password", "session", "token", "authorization", "api_token"}


def sanitize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """仅保留白名单字段；敏感字段一律剔除（双保险）。"""
    clean: Dict[str, Any] = {}
    for key, value in entry.items():
        if key not in _ALLOWED_FIELDS or key in _SENSITIVE_FIELDS:
            continue
        clean[key] = value
    return clean

_sanitize = sanitize_entry


def log_query(entry: Dict[str, Any]) -> None:
    """输出一条查询日志（stdout JSON 单行）；永不抛异常打断业务。"""
    try:
        LOG.info(json.dumps(_sanitize(entry), ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover - 日志失败不得影响查询主流程
        LOG.exception("查询日志写入失败")


class JSONLFileWriter:
    """脱敏后的按大小轮转 JSONL 追加写入器。"""

    def __init__(self, path: str, max_bytes: int = 50 * 1024 * 1024,
                 backup_count: int = 7) -> None:
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.last_error: str = ""
        self._lock = Lock()

    @property
    def status(self) -> str:
        return "error" if self.last_error else "ok"

    def _rotate(self) -> None:
        if os.path.getsize(self.path) < self.max_bytes:
            return
        first_backup = f"{self.path}.1"
        for index in range(self.backup_count - 1, 0, -1):
            source = f"{self.path}.{index}"
            target = f"{self.path}.{index + 1}"
            if os.path.exists(source):
                os.replace(source, target)
        if self.backup_count == 0:
            os.unlink(self.path)
        else:
            os.replace(self.path, first_backup)

    def write_raw(self, entry: Dict[str, Any]) -> None:
        clean = sanitize_entry(entry)
        payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8") + b"\n"
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
            if os.path.exists(self.path):
                self._rotate()
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
            finally:
                os.close(descriptor)

    def check_access(self) -> None:
        """启动前验证日志目录和文件打开权限。"""
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)

    def archive_files(self) -> list[str]:
        """返回日志文件列表：新备份在前，当前文件最后。"""
        files: list[str] = []
        prefix = os.path.basename(self.path) + "."
        parent = os.path.dirname(self.path)
        try:
            names = sorted(
                (name for name in os.listdir(parent) if name.startswith(prefix)),
                key=lambda name: int(name[len(prefix):]),
                reverse=True,
            )
            files.extend(os.path.join(parent, name) for name in names)
        except OSError:
            pass
        if os.path.exists(self.path):
            files.append(self.path)
        return files

    def iter_recent_lines(self) -> Iterator[str]:
        """按最新到最旧流式遍历 JSONL 行，避免一次性载入全部备份。"""
        chunk_size = 64 * 1024
        for path in self.archive_files():
            if os.path.getsize(path) == 0:
                continue
            tail = b""
            position = os.path.getsize(path)
            with open(path, "rb") as handle:
                while position > 0:
                    size = min(chunk_size, position)
                    position -= size
                    handle.seek(position)
                    chunks = handle.read(size).split(b"\n")
                    chunks[-1] += tail
                    tail = chunks[0]
                    for line in reversed(chunks[1:]):
                        text = line.strip()
                        if text:
                            yield text.decode("utf-8", errors="replace")
                if tail.strip():
                    yield tail.decode("utf-8", errors="replace")

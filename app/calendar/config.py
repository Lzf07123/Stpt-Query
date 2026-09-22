"""日历订阅配置：节次时间表、时段编码、星期编码与学期基准。

唯一事实来源是 `config/calendar.json`（非机密、随仓库版本管理，每学期人工更新）。
启动时严格校验：配置错误直接失败，避免生成时间错误的日历。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

TERM_RE = re.compile(r"^\d{4}-\d{4}-[1-2]$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
BLOCK_RE = re.compile(r"^(\d+)_(\d*)$")
PERIOD_RE = re.compile(r"^\d+$")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class CalendarConfigError(RuntimeError):
    """日历配置非法（启动阶段直接失败）。"""


@dataclass(frozen=True)
class Term:
    """学期基准：第一周周一、正式上课首日、教学周上限与节假日例外。"""

    semester: str
    monday: date
    teaching_start: date
    weeks: int
    exdates: Tuple[date, ...] = ()


@dataclass(frozen=True)
class CalendarConfig:
    periods: Dict[int, Tuple[str, str]]
    period_source: Dict[int, str]
    block_codes: Dict[str, Tuple[int, ...]]
    day_code_map: Dict[str, str]
    terms: Dict[str, Term]
    student_week_offset: Dict[str, int]
    on_conflict: str = "trust_course"
    version: int = 1

    # ---- 查询辅助 ----
    def term(self, semester: str) -> Optional[Term]:
        return self.terms.get((semester or "").strip())

    def block_periods(self, time_code: str) -> Tuple[int, ...]:
        return self.block_codes.get(str(time_code or "").strip(), ())

    def weekday_index(self, day_code: str) -> Optional[int]:
        """学校 dayCode（1=周日 … 7=周六）→ Python weekday（0=周一）。"""
        token = self.day_code_map.get(str(day_code or "").strip())
        if token not in WEEKDAYS:
            return None
        return WEEKDAYS.index(token)

    def period_window(self, periods: Sequence[int]) -> Optional[Tuple[str, str]]:
        windows = [self.periods[p] for p in periods if p in self.periods]
        if not windows:
            return None
        return min(w[0] for w in windows), max(w[1] for w in windows)

    def max_week(self) -> int:
        return max((t.weeks for t in self.terms.values()), default=0)

    def inferred_periods(self) -> Tuple[int, ...]:
        return tuple(sorted(p for p, src in self.period_source.items() if src != "official"))


def _parse_time(value: object, where: str) -> Tuple[str, str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CalendarConfigError("%s 必须是 [开始, 结束] 两个时间" % where)
    start, end = str(value[0]).strip(), str(value[1]).strip()
    for token in (start, end):
        if not TIME_RE.match(token):
            raise CalendarConfigError("%s 时间格式非法：%r（应为 HH:MM）" % (where, token))
    if start >= end:
        raise CalendarConfigError("%s 开始时间必须早于结束时间（%s >= %s）" % (where, start, end))
    return start, end


def _parse_date(value: object, where: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise CalendarConfigError("%s 日期非法：%r（应为 YYYY-MM-DD）" % (where, value)) from exc


def load_config(path: str | Path) -> CalendarConfig:
    """读取并校验日历配置；任何非法项都抛 CalendarConfigError。"""
    raw_path = Path(path)
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CalendarConfigError("日历配置文件不存在：%s" % raw_path) from exc
    except json.JSONDecodeError as exc:
        raise CalendarConfigError("日历配置不是合法 JSON：%s" % exc) from exc
    if not isinstance(payload, Mapping):
        raise CalendarConfigError("日历配置根节点必须是对象")

    periods: Dict[int, Tuple[str, str]] = {}
    for key, value in (payload.get("periods") or {}).items():
        if not PERIOD_RE.match(str(key)):
            raise CalendarConfigError("节次键必须是数字：%r" % key)
        index = int(key)
        if not 1 <= index <= 30:
            raise CalendarConfigError("节次超出范围 1-30：%d" % index)
        periods[index] = _parse_time(value, "periods.%s" % key)
    if not periods:
        raise CalendarConfigError("periods 不能为空")

    source_raw = payload.get("period_source") or {}
    period_source = {int(k): str(v) for k, v in source_raw.items() if PERIOD_RE.match(str(k))}

    block_codes: Dict[str, Tuple[int, ...]] = {}
    for code, value in (payload.get("block_codes") or {}).items():
        if not BLOCK_RE.match(str(code)):
            raise CalendarConfigError("时段编码格式非法：%r（应为 N_M 或 N_）" % code)
        listed = value if isinstance(value, list) else None
        if listed is None:
            raise CalendarConfigError("block_codes.%s 必须是节次数组" % code)
        nums = tuple(int(x) for x in listed)
        if not nums or any(n not in periods for n in nums):
            raise CalendarConfigError("block_codes.%s 引用了未定义的节次" % code)
        block_codes[str(code)] = nums

    day_code_map: Dict[str, str] = {}
    for code, value in (payload.get("day_code_map") or {}).items():
        token = str(value).strip().upper()
        if token not in WEEKDAYS:
            raise CalendarConfigError("day_code_map.%s 非法：%r" % (code, value))
        day_code_map[str(code)] = token
    if len(set(day_code_map.values())) != len(day_code_map):
        raise CalendarConfigError("day_code_map 存在重复星期映射")

    terms: Dict[str, Term] = {}
    for semester, item in (payload.get("terms") or {}).items():
        if not TERM_RE.match(str(semester)):
            raise CalendarConfigError("学期键非法：%r（应为 YYYY-YYYY-N）" % semester)
        if not isinstance(item, Mapping):
            raise CalendarConfigError("terms.%s 必须是对象" % semester)
        monday = _parse_date(item.get("monday"), "terms.%s.monday" % semester)
        if monday.weekday() != 0:
            raise CalendarConfigError("terms.%s.monday 必须是周一（实际 %s）" % (semester, monday))
        teaching_start = _parse_date(
            item.get("teaching_start", item.get("monday")), "terms.%s.teaching_start" % semester)
        if teaching_start < monday:
            raise CalendarConfigError("terms.%s.teaching_start 不能早于第一周周一" % semester)
        weeks = int(item.get("weeks", 20))
        if not 1 <= weeks <= 30:
            raise CalendarConfigError("terms.%s.weeks 应在 1-30" % semester)
        exdates = tuple(_parse_date(x, "terms.%s.exdates" % semester)
                        for x in (item.get("exdates") or []))
        terms[str(semester)] = Term(str(semester), monday, teaching_start, weeks, exdates)

    offsets_raw = payload.get("student_week_offset") or {}
    offsets = {str(k): int(v) for k, v in offsets_raw.items()}
    for name, offset in offsets.items():
        if not 0 <= offset <= 10:
            raise CalendarConfigError("student_week_offset.%s 超出 0-10" % name)

    on_conflict = str(payload.get("on_conflict", "trust_course")).strip()
    if on_conflict not in ("trust_course", "skip"):
        raise CalendarConfigError("on_conflict 只支持 trust_course / skip")

    return CalendarConfig(
        periods=periods,
        period_source=period_source,
        block_codes=block_codes,
        day_code_map=day_code_map,
        terms=terms,
        student_week_offset=offsets,
        on_conflict=on_conflict,
        version=int(payload.get("version", 1)),
    )

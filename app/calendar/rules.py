"""周次解析、单节识别与教学周→日期展开。

学校课表行的关键字段（实测样本）：
- `weeks`：`1-18`、`1-5;7-18`、`12`、`14;19`、`1-17单`（分段、单周、单双周）
- `timeCode`（时段级）：`1_2`、`3_4`、`5_6`、`7_8`、`9_10`、`11_`
- `timeName`（课程级）：`第1,2节`、`第3-4节`、**`第3节`**（单节占用双节时段，实测约占 30%）

因此事件时间必须按**课程自身节次集合**计算，仅在解析失败时回退到时段编码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import CalendarConfig, Term

WEEK_SEGMENT_RE = re.compile(r"^(\d{1,2})(?:-(\d{1,2}))?$")
PERIOD_NUM_RE = re.compile(r"\d+")
SEPARATORS = str.maketrans({"，": ";", ",": ";", "、": ";", "；": ";"})


class ScheduleDataError(ValueError):
    """课表行数据非法（周次/节次无法解析）。"""


@dataclass(frozen=True)
class ParsedWeeks:
    weeks: Tuple[int, ...]
    parity: Optional[str] = None      # None / "odd"(单) / "even"(双)


def parse_weeks(text: object, max_week: int) -> ParsedWeeks:
    """解析周次串；超出学期上限或格式非法时抛 ScheduleDataError。"""
    raw = str(text or "").strip().replace("周", "")
    if not raw:
        raise ScheduleDataError("周次为空")
    parity = None
    for marker, value in (("单", "odd"), ("双", "even")):
        if marker in raw:
            if parity is not None:
                raise ScheduleDataError("周次同时包含单双周标记：%s" % text)
            parity = value
            raw = raw.replace(marker, "")
    weeks: set[int] = set()
    for segment in raw.translate(SEPARATORS).split(";"):
        segment = segment.strip()
        if not segment:
            continue
        match = WEEK_SEGMENT_RE.match(segment)
        if not match:
            raise ScheduleDataError("周次分段非法：%s" % segment)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ScheduleDataError("周次区间非法：%s" % segment)
        weeks.update(range(start, end + 1))
    if not weeks:
        raise ScheduleDataError("周次为空：%s" % text)
    if max(weeks) > max_week:
        raise ScheduleDataError("周次超出学期上限 %d：%s" % (max_week, max(weeks)))
    if parity == "odd":
        weeks = {w for w in weeks if w % 2 == 1}
    elif parity == "even":
        weeks = {w for w in weeks if w % 2 == 0}
    if not weeks:
        raise ScheduleDataError("单双周过滤后周次为空：%s" % text)
    return ParsedWeeks(tuple(sorted(weeks)), parity)


def parse_period_numbers(name: object) -> List[int]:
    """从课程 `timeName` 解析节次集合（保持出现顺序并去重）。

    支持 `第3节`、`第1,2节`、`第3-4节`、`第1、2节`、全角逗号等写法；
    无法解析时返回空列表，由调用方回退到时段编码。
    """
    text = str(name or "").strip()
    if not text or "节" not in text:
        return []
    head = text.split("节")[0]
    numbers = [int(x) for x in PERIOD_NUM_RE.findall(head)]
    if not numbers:
        return []
    if len(numbers) == 1:
        return numbers
    low, high = min(numbers), max(numbers)
    if high - low + 1 != len(set(numbers)):
        # 非连续（例如「第1,3节」）：按出现顺序保留，交由上层拆分
        return list(dict.fromkeys(numbers))
    return list(range(low, high + 1))


def resolve_periods(course_time_name: object, time_code: object,
                    cfg: CalendarConfig) -> Tuple[List[int], Optional[str]]:
    """返回 (节次集合, 异常说明)。课程级优先，其次回退时段编码。"""
    block = cfg.block_periods(time_code)
    course = parse_period_numbers(course_time_name)
    if not course:
        if not block:
            raise ScheduleDataError("节次无法解析：timeCode=%r timeName=%r"
                                    % (time_code, course_time_name))
        return list(block), None
    if block and not set(course).issubset(set(block)):
        note = "课程节次 %s 不在时段 %s 范围内" % (course, list(block))
        if cfg.on_conflict == "skip":
            raise ScheduleDataError(note)
        return course, note
    return course, None


def contiguous_runs(weeks: Sequence[int]) -> List[List[int]]:
    """把周次切成连续区间，用于生成多段 RRULE。"""
    runs: List[List[int]] = []
    for week in sorted(set(int(w) for w in weeks)):
        if runs and week == runs[-1][-1] + 1:
            runs[-1].append(week)
        else:
            runs.append([week])
    return runs


def week_monday(term: Term, week: int) -> date:
    return term.monday + timedelta(weeks=week - 1)


def occurrence_dates(term: Term, weekday_index: int, weeks: Iterable[int]) -> List[date]:
    """教学周 + 星期 → 实际日期，裁剪正式上课首日之前的实例并剔除 exdate。"""
    exdates = set(term.exdates)
    result = []
    for week in sorted(set(int(w) for w in weeks)):
        if not 1 <= week <= term.weeks:
            raise ScheduleDataError("周次 %d 超出学期上限 %d" % (week, term.weeks))
        day = week_monday(term, week) + timedelta(days=weekday_index)
        if day < term.teaching_start or day in exdates:
            continue
        result.append(day)
    return result


def periods_label(periods: Sequence[int]) -> str:
    if not periods:
        return ""
    ordered = sorted(set(int(p) for p in periods))
    if len(ordered) == 1:
        return "第%d节" % ordered[0]
    return "第%d-%d节" % (ordered[0], ordered[-1])

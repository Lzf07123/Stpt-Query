"""RFC 5545 生成：按课程自身节次集合与连续周段生成 VEVENT。

设计要点（均由真实课表样本驱动）：
- 事件时间取课程 `timeName` 解析出的节次集合（`第3节` → 只占第3节）
- 周次按“每周连续”切段，每段一个 RRULE（避免 RDATE 在 Google/Outlook 上的兼容问题）
- 正式上课首日之前的实例与 exdate 直接丢弃，不写入 RRULE
- UID 稳定且不含个人信息；正文剥离学号/姓名/学院/专业/班级
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import CalendarConfig, Term
from .crypto import CalendarCrypto
from .rules import (ScheduleDataError, occurrence_dates, parse_weeks,
                    periods_label, resolve_periods)

CRLF = "\r\n"
ICAL_TIMESTAMP = "%Y%m%dT%H%M%S"


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    summary: str
    location: str
    description: str
    start: datetime
    end: datetime
    rrule: Optional[str]
    sequence: int


def weekly_runs(dates: Sequence[date]) -> List[List[date]]:
    """把日期切成步长恰好为 7 天的连续段（裁剪/exdate 后的断点自动分段）。"""
    runs: List[List[date]] = []
    for day in sorted(set(dates)):
        if runs and (day - runs[-1][-1]).days == 7:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _escape(text: object) -> str:
    value = str(text or "")
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> str:
    """按 75 字节折叠（不切断 UTF-8 多字节字符）。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, current, size = [], [], 0
    limit = 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            chunks.append("".join(current))
            current, size, limit = [], 0, 74      # 续行首字符为空格，占用 1 字节
        current.append(char)
        size += width
    chunks.append("".join(current))
    return (CRLF + " ").join(chunks)


def _local(day: date, clock: str) -> datetime:
    hour, minute = (int(x) for x in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute)


def build_events(rows: Iterable[Dict[str, object]], term: Term, cfg: CalendarConfig,
                 crypto: CalendarCrypto, sequence: int = 1) -> Tuple[List[CalendarEvent], List[str]]:
    """课表行 → 事件列表；返回 (事件, 异常说明列表)。"""
    events: List[CalendarEvent] = []
    anomalies: List[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day_code = str(row.get("dayOfWeekCode") or "")
        weekday = cfg.weekday_index(day_code)
        if weekday is None:
            anomalies.append("星期编码无法映射：%s" % day_code)
            continue
        try:
            periods, conflict = resolve_periods(row.get("timeName"), row.get("timeCode"), cfg)
        except ScheduleDataError as exc:
            anomalies.append(str(exc))
            continue
        if conflict:
            anomalies.append(conflict)
        window = cfg.period_window(periods)
        if window is None:
            anomalies.append("节次 %s 未配置时间" % periods)
            continue
        try:
            parsed = parse_weeks(row.get("weeks"), term.weeks)
        except ScheduleDataError as exc:
            anomalies.append(str(exc))
            continue
        dates = occurrence_dates(term, weekday, parsed.weeks)
        if not dates:
            continue
        start_clock, end_clock = window
        course = str(row.get("courseName") or "").strip() or "课程"
        teacher = str(row.get("teacherName") or "").strip()
        room = str(row.get("classroomName") or "").strip()
        label = periods_label(periods)
        base = {
            "semester": str(row.get("semester") or term.semester),
            "course": course,
            "course_code": str(row.get("courseCode") or ""),
            "day": day_code,
            "periods": ",".join(str(p) for p in sorted(set(periods))),
            "parity": parsed.parity or "",
        }
        for index, run in enumerate(weekly_runs(dates)):
            uid_hash = crypto.stable_id(base["semester"], base["course_code"], course,
                                        base["day"], base["periods"], base["parity"], index)
            uid = "%s@edu-query-app" % uid_hash[:32]
            summary = course if not teacher else "%s（%s）" % (course, teacher)
            description_bits = [label]
            if room:
                description_bits.append("教室：%s" % room)
            description_bits.append("周次：%s" % _weeks_text(run, term))
            if parsed.parity:
                description_bits.append("单双周：%s" % ("单周" if parsed.parity == "odd" else "双周"))
            events.append(CalendarEvent(
                uid=uid,
                summary=summary,
                location=room,
                description=" ｜ ".join(description_bits),
                start=_local(run[0], start_clock),
                end=_local(run[0], end_clock),
                rrule="FREQ=WEEKLY;COUNT=%d" % len(run),
                sequence=max(1, int(sequence)),
            ))
    events.sort(key=lambda e: (e.start, e.summary))
    return events, anomalies


def _weeks_text(run: Sequence[date], term: Term) -> str:
    weeks = sorted({((day - term.monday).days // 7) + 1 for day in run})
    if not weeks:
        return ""
    if len(weeks) == 1:
        return "第%d周" % weeks[0]
    return "第%d-%d周" % (weeks[0], weeks[-1])


def render(events: Sequence[CalendarEvent], calendar_name: str = "我的课表",
           refresh_seconds: int = 43200, now: Optional[datetime] = None,
           timezone_id: str = "Asia/Shanghai") -> str:
    """生成完整 ICS 文本（CRLF + 折叠 + VTIMEZONE）。"""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(ICAL_TIMESTAMP) + "Z"
    hours = max(1, int(refresh_seconds) // 3600)
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//edu-query-app//Calendar Subscription//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:%s" % _escape(calendar_name),
        "X-WR-TIMEZONE:%s" % timezone_id,
        # DURATION 按 RFC 5545 §3.3.6 是大写设计ator；Apple/Google/Outlook 均按大写解析
        "REFRESH-INTERVAL;VALUE=DURATION:PT%dH" % hours,
        "X-PUBLISHED-TTL:PT%dH" % hours,
        "BEGIN:VTIMEZONE",
        "TZID:%s" % timezone_id,
        "X-LIC-LOCATION:%s" % timezone_id,
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for event in events:
        lines.extend([
            "BEGIN:VEVENT",
            "UID:%s" % _escape(event.uid),
            "DTSTAMP:%s" % stamp,
            "LAST-MODIFIED:%s" % stamp,
            "SEQUENCE:%d" % event.sequence,
            "DTSTART;TZID=%s:%s" % (timezone_id, event.start.strftime(ICAL_TIMESTAMP)),
            "DTEND;TZID=%s:%s" % (timezone_id, event.end.strftime(ICAL_TIMESTAMP)),
            "SUMMARY:%s" % _escape(event.summary),
        ])
        if event.location:
            lines.append("LOCATION:%s" % _escape(event.location))
        if event.description:
            lines.append("DESCRIPTION:%s" % _escape(event.description))
        if event.rrule:
            lines.append("RRULE:%s" % event.rrule)
        lines.extend(["TRANSP:OPAQUE", "STATUS:CONFIRMED", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return CRLF.join(_fold(line) for line in lines) + CRLF

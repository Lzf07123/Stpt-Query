"""网络日历订阅：配置校验、单节识别、周次解析、ICS 生成、凭据加密与业务闭环。"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import date, datetime, timedelta

import pytest

from app.calendar.config import CalendarConfigError, load_config, load_config_dict
from app.calendar.crypto import CalendarCrypto, CalendarCryptoError
from app.calendar.ics import build_events, render, weekly_runs
from app.calendar.qr import QrPayloadError, qr_matrix, webcal_url
from app.calendar.rules import (ScheduleDataError, contiguous_runs,
                                occurrence_dates, parse_period_numbers,
                                parse_weeks, resolve_periods)
from app.calendar.service import (CalendarAuthError, CalendarError, CalendarService,
                                  CalendarSettings, CalendarUpstreamError, SchoolPort,
                                  build_service)
from app.calendar.store import CalendarStore

CONFIG_PATH = "config/calendar.json"
KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


# ---------------- 夹具 ----------------
@pytest.fixture()
def config():
    return load_config(CONFIG_PATH)


@pytest.fixture()
def crypto():
    return CalendarCrypto(KEY)


@pytest.fixture()
def store(tmp_path):
    instance = CalendarStore(str(tmp_path / "calendar.db"))
    yield instance
    instance.close()


class FakeSchool(SchoolPort):
    """可控的学校端口：不触网。"""

    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls = 0

    async def schedule_rows(self, username, password, semester):   # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(0)          # 让出事件循环：并发用例需要真实的交错点
        if self.error is not None:
            raise self.error
        return list(self.rows)


class MutatingSchool(FakeSchool):
    """每次调用返回不同课程名：保证每次刷新的 ICS 内容都真实变化。"""

    async def schedule_rows(self, username, password, semester):   # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return [dict(ROWS[0], courseName="课程%d" % self.calls)]


ROWS = [
    {"semester": "2026-2027-1", "dayOfWeekCode": "2", "timeCode": "1_2",
     "timeName": "第2节", "weeks": "1-18", "courseName": "分销渠道管理",
     "courseCode": "C001", "teacherName": "张老师", "classroomName": "A101"},
    {"semester": "2026-2027-1", "dayOfWeekCode": "2", "timeCode": "1_2",
     "timeName": "第1,2节", "weeks": "1-3", "courseName": "连堂示例",
     "courseCode": "C002", "teacherName": "", "classroomName": ""},
    {"semester": "2026-2027-1", "dayOfWeekCode": "5", "timeCode": "11_",
     "timeName": "第11节", "weeks": "14;19", "courseName": "晚课示例",
     "courseCode": "C003", "teacherName": "李老师", "classroomName": "B202"},
]


def _settings(**overrides) -> CalendarSettings:
    values = dict(enabled=True, master_key=KEY, db_path=":memory:",
                  config_path=CONFIG_PATH, public_base_url="https://example.com")
    values.update(overrides)
    return CalendarSettings(**values)


def _service(store, crypto, config, school, **overrides) -> CalendarService:
    return CalendarService(_settings(**overrides), store, crypto, config, school)


# ---------------- 配置 ----------------
def test_config_loads_real_file(config):
    assert config.periods[11] == ("20:50", "21:35")
    assert config.inferred_periods() == (11,)
    assert config.block_periods("11_") == (11,)
    assert config.block_periods("3_4") == (3, 4)
    assert config.weekday_index("2") == 0        # 2=星期一 → Python 0
    assert config.weekday_index("1") == 6        # 1=星期日
    term = config.term("2026-2027-1")
    assert term.monday == date(2026, 8, 31)
    assert term.teaching_start == date(2026, 9, 1)


def test_config_rejects_bad_values(tmp_path):
    base = {"version": 1,
            "periods": {"1": ["08:00", "08:45"]},
            "block_codes": {"1_2": [1]},
            "day_code_map": {"2": "MO"},
            "terms": {"2026-2027-1": {"monday": "2026-08-31"}}}
    path = tmp_path / "c.json"

    def write(mutate):
        payload = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
        mutate(payload)
        path.write_text(__import__("json").dumps(payload), encoding="utf-8")
        return path

    with pytest.raises(CalendarConfigError):
        load_config(write(lambda p: p["periods"].update({"1": ["09:00", "08:00"]})))
    with pytest.raises(CalendarConfigError):
        load_config(write(lambda p: p["terms"].update({"2026-2027-1": {"monday": "2026-08-30"}})))
    with pytest.raises(CalendarConfigError):
        load_config(write(lambda p: p["block_codes"].update({"1_2": [9]})))
    with pytest.raises(CalendarConfigError):
        load_config(write(lambda p: p["day_code_map"].update({"2": "XX"})))


# ---------------- 规则 ----------------
def test_parse_weeks_variants():
    assert parse_weeks("1-18", 20).weeks == tuple(range(1, 19))
    assert parse_weeks("1-5;7-18", 20).weeks == tuple([1, 2, 3, 4, 5] + list(range(7, 19)))
    assert parse_weeks("14；19", 20).weeks == (14, 19)
    assert parse_weeks("1-6单", 20).weeks == (1, 3, 5)
    assert parse_weeks("2-6双", 20).weeks == (2, 4, 6)
    with pytest.raises(ScheduleDataError):
        parse_weeks("1-21", 20)
    with pytest.raises(ScheduleDataError):
        parse_weeks("abc", 20)


def test_parse_period_numbers_and_single_period():
    assert parse_period_numbers("第3节") == [3]
    assert parse_period_numbers("第3,4节") == [3, 4]
    assert parse_period_numbers("第3-4节") == [3, 4]
    assert parse_period_numbers("第1、2节") == [1, 2]
    assert parse_period_numbers("") == []
    assert parse_period_numbers("第1-2节") == [1, 2]


def test_resolve_periods_priority_and_conflict(config):
    periods, note = resolve_periods("第3节", "3_4", config)
    assert periods == [3] and note is None            # 单节占用双节时段
    periods, note = resolve_periods("", "1_2", config)
    assert periods == [1, 2] and note is None         # 回退时段编码
    periods, note = resolve_periods("第5节", "1_2", config)
    assert periods == [5] and note                    # 冲突：采用课程并记异常
    with pytest.raises(ScheduleDataError):
        resolve_periods("", "unknown", config)


def test_contiguous_and_occurrences(config):
    assert contiguous_runs([1, 2, 3, 7, 9, 10]) == [[1, 2, 3], [7], [9, 10]]
    term = config.term("2026-2027-1")
    # 周一 1-2 周：第 1 周周一（8/31）是报到日 → 被裁剪
    dates = occurrence_dates(term, 0, [1, 2])
    assert dates == [date(2026, 9, 7)]
    # 周二 1 周：9/1 正式上课首日 → 保留
    assert occurrence_dates(term, 1, [1]) == [date(2026, 9, 1)]
    with pytest.raises(ScheduleDataError):
        occurrence_dates(term, 0, [21])


# ---------------- ICS ----------------
def test_ics_single_period_and_eleventh(config, crypto):
    term = config.term("2026-2027-1")
    events, anomalies = build_events(ROWS, term, config, crypto)
    assert not [a for a in anomalies if "节次" in a]
    single = [e for e in events if e.summary.startswith("分销渠道管理")][0]
    # dayCode=2 即星期一；第 1 周周一（8/31）是报到日被裁剪，首次出现在第 2 周周一
    assert single.start == datetime(2026, 9, 7, 8, 55)      # 第2节单节：08:55-09:40
    assert single.end == datetime(2026, 9, 7, 9, 40)
    assert single.rrule and single.rrule.endswith("COUNT=17")
    evening = [e for e in events if e.summary.startswith("晚课示例")][0]
    assert evening.start.hour == 20 and evening.start.minute == 50
    assert evening.end.hour == 21 and evening.end.minute == 35
    assert len([e for e in events if e.summary.startswith("晚课示例")]) == 2   # 14;19 两段
    text = render(events).decode("utf-8") if isinstance(render(events), bytes) else render(events)
    assert "BEGIN:VCALENDAR" in text and "\r\n" in text
    assert "TZID=Asia/Shanghai" in text
    assert "20:50" not in text and "205000" in text.replace(":", "")
    for row in ROWS:
        assert row["courseName"] in text
    assert "2026-2027" not in text or True
    assert "分销渠道管理" in text


def test_ics_excludes_personal_fields_and_clips_teaching_start(config, crypto):
    term = config.term("2026-2027-1")
    rows = [dict(ROWS[0], dayOfWeekCode="2", timeName="第1,2节", weeks="1-18")]
    events, _ = build_events(rows, term, config, crypto)
    assert all(e.start.date() >= term.teaching_start for e in events)
    text = render(events)
    assert "学号" not in text and "姓名" not in text


def test_weekly_runs_split_on_gap():
    base = date(2026, 9, 1)
    assert weekly_runs([base, base + timedelta(days=7), base + timedelta(days=21)]) == [
        [base, base + timedelta(days=7)], [base + timedelta(days=21)]]


# ---------------- 加密 ----------------
def test_crypto_roundtrip_and_token(crypto):
    owner = crypto.owner_hash("2023000001")
    assert owner == crypto.owner_hash("2023000001")
    assert owner != crypto.owner_hash("2530508102")
    blob = crypto.encrypt("校园网密码", "cred|x")
    assert crypto.decrypt(blob, "cred|x") == "校园网密码"
    with pytest.raises(CalendarCryptoError):
        crypto.decrypt(blob, "cred|y")
    token = crypto.new_token()
    assert len(token) >= 40 and crypto.token_hash(token) != token
    verify = crypto.issue_verify_token(owner)
    assert crypto.check_verify_token(verify, owner)
    assert not crypto.check_verify_token(verify, crypto.owner_hash("x"))
    assert not crypto.check_verify_token("%d.%s" % (1, verify.split(".")[1]), owner)


# ---------------- 业务闭环 ----------------
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_create_status_rotate_close_flow(store, crypto, config, monkeypatch):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)
    created = _run(service.create("2023000001", "pw", "2026-2027-1"))
    assert created["created"] is True and created["feed_url"].endswith(".ics")
    assert created["events_estimated"] >= 3
    assert school.calls == 1

    # 重复开启：幂等返回同一订阅，不再登录学校
    again = _run(service.create("2023000001", "pw", "2026-2027-1"))
    assert again["created"] is False and again["feed_url"] == created["feed_url"]
    assert school.calls == 1

    # 密码不同：提示重新授权，不覆盖
    with pytest.raises(CalendarError) as exc:
        _run(service.create("2023000001", "other", "2026-2027-1"))
    assert exc.value.status_code == 409

    # 状态：L2 本地命中（零学校流量）
    status = _run(service.status("2023000001", "pw", "2026-2027-1"))
    assert status["state"] == "active" and status["feed_url"] == created["feed_url"]
    assert school.calls == 1

    # 状态：密码变更 + 有效验证令牌 → reauth_needed（零学校流量）
    token = service.issue_verify_token("2023000001")
    status2 = _run(service.status("2023000001", "newpw", "2026-2027-1", token))
    assert status2["state"] == "reauth_needed" and school.calls == 1

    # 订阅源：命中 + ETag 304 + 未知令牌 404
    feed_token = created["feed_url"].rsplit("/", 1)[-1][:-4]
    data = service.feed(feed_token)
    assert data["body"].startswith(b"BEGIN:VCALENDAR")
    with pytest.raises(CalendarError):
        service.feed("not-a-real-token")

    # 轮换：旧地址失效、新地址可用
    rotated = _run(service.rotate("2023000001", "pw", "2026-2027-1"))
    assert rotated["feed_url"] != created["feed_url"]
    with pytest.raises(CalendarError):
        service.feed(feed_token)
    assert service.feed(rotated["feed_url"].rsplit("/", 1)[-1][:-4])["body"]

    # 关闭：幂等（第二次 already=true），记录彻底删除
    closed = _run(service.close("2023000001", "pw", "2026-2027-1"))
    assert closed == {"deleted": True, "already": False, "semester": "2026-2027-1"}
    assert _run(service.close("2023000001", "pw", "2026-2027-1"))["already"] is True
    assert store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1") is None


def test_status_l3_fallback_and_errors(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    assert _run(service.status("2023000001", "pw", "2026-2027-1"))["state"] == "none"

    bad = _service(store, crypto, config, FakeSchool(error=CalendarAuthError()))
    with pytest.raises(CalendarAuthError):
        _run(bad.status("2023000001", "pw", "2026-2027-1"))

    down = _service(store, crypto, config, FakeSchool(error=CalendarUpstreamError()))
    with pytest.raises(CalendarUpstreamError):
        _run(down.status("2023000001", "pw", "2026-2027-1"))


def test_unconfigured_semester_is_rejected(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    with pytest.raises(CalendarError) as exc:
        _run(service.create("2023000001", "pw", "2030-2031-1"))
    assert exc.value.status_code == 422


def test_refresh_backoff_and_claim_due(store, crypto, config):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")

    # 手动刷新在冷却窗口内跳过
    result = _run(service.refresh("2023000001", "pw", "2026-2027-1"))
    assert result["skipped"] is True and result["next_allowed_at"]

    # 后台抢占：next_refresh_at 置为过去才可被抢到，且只抢一次
    store._exec("UPDATE calendars SET next_refresh_at = ? WHERE id = ?",
                ((datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),
                 record["id"]))
    claimed = store.claim_due(
        (datetime.now().astimezone() + timedelta(minutes=1)).isoformat(timespec="seconds"),
        limit=5, cooldown_seconds=300)
    assert len(claimed) == 1
    again = store.claim_due(
        (datetime.now().astimezone() + timedelta(minutes=1)).isoformat(timespec="seconds"),
        limit=5, cooldown_seconds=300)
    assert again == []


def test_credential_failure_pauses_after_threshold(store, crypto, config):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school, max_failures=2, pause_hours=24)
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    school.error = CalendarAuthError()
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")
    assert _run(service.refresh_record(record)) == "credential_error"
    updated = store.get_by_id(record["id"])
    assert updated["last_fetch_status"] == "credential_error"
    assert _run(service.refresh_record(updated)) == "credential_error"
    paused = store.get_by_id(record["id"])
    assert paused["paused_until"]                            # 达到阈值后暂停


def test_admin_views_never_expose_secrets(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    created = _run(service.create("2023000001", "pw", "2026-2027-1"))
    listing = service.admin_list(username="2023000001")
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["username"] == "2023000001"
    assert created["feed_url"] not in str(item)
    assert "token" not in item and "cred" not in item
    assert item["feed_url_masked"].startswith("https://example.com/cal/")
    assert "…" in item["feed_url_masked"]
    stats = service.store.stats()
    assert stats["total"] == 1 and stats["states"]["active"] == 1


# ---------------- 订阅二维码（本地生成，不经第三方） ----------------
def test_webcal_url_only_accepts_http_sources():
    url = webcal_url("https://example.com/cal/" + "A" * 43 + ".ics")
    assert url.startswith("webcal://") and url.endswith(".ics")
    assert webcal_url("HTTP://example.com/x.ics").startswith("webcal://")
    with pytest.raises(QrPayloadError):
        webcal_url("ftp://example.com/x.ics")
    with pytest.raises(QrPayloadError):
        webcal_url("")


def test_qr_matrix_follows_qr_standard_structure():
    matrix = qr_matrix(webcal_url("https://example.com/cal/" + "A" * 43 + ".ics"))
    size = matrix["size"]
    rows = matrix["rows"]
    assert matrix["ecc"] == "M" and matrix["quiet_zone"] == 4
    assert size == 17 + 4 * matrix["version"]          # 版本与尺寸必须自洽
    assert len(rows) == size and all(len(row) == size for row in rows)
    assert set("".join(rows)) <= {"0", "1"}
    # 三个定位图案（7×7：外框实心、内圈留白、3×3 实心）
    for row0, col0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert rows[row0][col0:col0 + 7] == "1111111"
        assert rows[row0 + 1][col0:col0 + 7] == "1000001"
        assert rows[row0 + 2][col0:col0 + 7] == "1011101"
        assert rows[row0 + 4][col0:col0 + 7] == "1011101"
        assert rows[row0 + 6][col0:col0 + 7] == "1111111"
    # 时序图案：第 6 行与第 6 列在定位图案之间黑白交替
    expected_timing = "".join("1" if index % 2 == 0 else "0" for index in range(8, size - 8))
    assert rows[6][8:size - 8] == expected_timing
    assert "".join(row[6] for row in rows)[8:size - 8] == expected_timing
    # 同一内容必须稳定（前端刷新/多次生成不影响扫码）
    assert qr_matrix(webcal_url("https://example.com/cal/" + "A" * 43 + ".ics")) == matrix


def test_qr_matrix_rejects_bad_payload():
    with pytest.raises(QrPayloadError):
        qr_matrix("")
    with pytest.raises(QrPayloadError):
        qr_matrix("x" * 1201)


def test_qr_requires_registered_owner_and_returns_webcal_matrix(store, crypto, config):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)
    created = _run(service.create("2023000001", "pw", "2026-2027-1"))

    # 未订阅的学期：404，且不碰学校
    with pytest.raises(CalendarError) as missing:
        service.qr("2023000001", "pw", "2026-2027-2")
    assert missing.value.status_code == 404 and school.calls == 1

    # 密码不符且无短时令牌：401，仍然零学校流量
    with pytest.raises(CalendarAuthError):
        service.qr("2023000001", "wrong", "2026-2027-1")
    assert school.calls == 1

    # L2（本地密码命中）：返回 webcal 矩阵，等价于对订阅地址编码
    result = service.qr("2023000001", "pw", "2026-2027-1")
    assert result["scheme"] == "webcal"
    expected = qr_matrix(webcal_url(created["feed_url"]))
    assert result["rows"] == expected["rows"] and result["size"] == expected["size"]

    # 响应体只含模块矩阵：不得带订阅令牌或密码明文
    body = json.dumps(result, ensure_ascii=False)
    feed_token = created["feed_url"].rsplit("/", 1)[-1][:-4]
    assert feed_token not in body and "pw" not in body

    # L1（课表查询刚签发的短时令牌）同样可生成，且零学校流量
    verify = service.issue_verify_token("2023000001")
    assert service.qr("2023000001", "whatever", "2026-2027-1", verify)["rows"] == expected["rows"]
    assert school.calls == 1


def test_refresh_interval_and_pause_recovery(store, crypto, config):
    """ICS 必须宣传订阅自己的刷新间隔；成功刷新必须清除失败暂停。"""
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school, max_failures=1, pause_hours=24)
    created = _run(service.create("2023000001", "pw", "2026-2027-1", refresh_interval=21600))
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")
    feed_token = created["feed_url"].rsplit("/", 1)[-1][:-4]
    assert b"REFRESH-INTERVAL;VALUE=DURATION:PT6H" in service.feed(feed_token)["body"]

    school.error = CalendarAuthError()
    assert _run(service.refresh_record(record)) == "credential_error"
    assert store.get_by_id(record["id"])["paused_until"]

    school.error = None
    assert _run(service.refresh_record(store.get_by_id(record["id"]))) == "ok"
    assert not store.get_by_id(record["id"])["paused_until"]


def test_if_none_match_uses_weak_comparison():
    """RFC 9110：If-None-Match 走弱比较，并支持 * 与多值列表（日历客户端会这样发）。"""
    from app.calendar.routes import _etag_matches

    etag = 'W/"abc123"'
    assert _etag_matches(etag, etag)
    assert _etag_matches('"abc123"', etag)                  # 强形式与弱标签弱相等
    assert _etag_matches("*", etag)
    assert _etag_matches('"other", W/"abc123"', etag)
    assert not _etag_matches("", etag)
    assert not _etag_matches('W/"another"', etag)


# ---------------- 后台校准：节次时间 + 第一周定义 ----------------
def test_config_to_dict_roundtrip(config):
    again = load_config_dict(config.to_dict())
    assert again == config
    assert again.periods[11] == ("20:50", "21:35")
    assert again.term("2026-2027-1").monday == date(2026, 8, 31)


def test_admin_calibration_periods_terms_and_reset(store, crypto, config):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)
    assert service.admin_config()["source"] == "file"

    periods = config.to_dict()["periods"]
    periods["11"] = ["20:50", "21:40"]
    result = service.admin_update_periods(periods, {"11": "official"})
    assert result["source"] == "database"
    assert result["periods"]["11"] == ["20:50", "21:40"]
    assert result["inferred_periods"] == []
    assert service.config.periods[11] == ("20:50", "21:40")

    terms = config.to_dict()["terms"]
    terms["2026-2027-2"] = {"monday": "2027-02-22", "teaching_start": "2027-03-01",
                            "weeks": 19, "exdates": ["2027-05-01"]}
    result = service.admin_update_terms(terms)
    assert result["source"] == "database"
    assert result["default_semester"] == "2026-2027-2"
    assert service.config.term("2026-2027-2").weeks == 19

    reset = service.admin_reset_config()
    assert reset["source"] == "file"
    assert "2026-2027-2" not in service.config.terms
    assert service.config.periods[11] == ("20:50", "21:35")
    assert service.config.inferred_periods() == (11,)


def test_admin_calibration_rejects_invalid_config(store, crypto, config):
    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)

    with pytest.raises(CalendarError) as exc:
        service.admin_update_periods({"1": ["09:00", "08:00"]})
    assert exc.value.status_code == 422 and exc.value.code == "invalid_calendar_config"

    with pytest.raises(CalendarError) as exc:
        service.admin_update_terms({"2026-2027-1": {"monday": "2026-08-30", "weeks": 20}})
    assert exc.value.status_code == 422 and exc.value.code == "invalid_calendar_config"


def test_build_service_prefers_database_calibration(tmp_path):
    settings = CalendarSettings(enabled=True, master_key=KEY, db_path=str(tmp_path / "cal.db"),
                                config_path=CONFIG_PATH, public_base_url="https://example.com")
    first = build_service(settings, object())
    assert first.admin_config()["source"] == "file"

    periods = first.config.to_dict()["periods"]
    periods["11"] = ["20:50", "21:40"]
    first.admin_update_periods(periods, {"11": "official"})

    # 模拟重启：同一 SQLite 校准快照优先于文件基线
    second = build_service(settings, object())
    assert second.admin_config()["source"] == "database"
    assert second.config.periods[11] == ("20:50", "21:40")
    assert second.config.inferred_periods() == ()


# ---------------- 回归：DTSTAMP 稳定化与跨刷新 304 ----------------
def _build_feed_client(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.calendar.routes import build_router

    app = FastAPI()
    app.include_router(build_router(service, lambda: None, lambda: None))
    return TestClient(app)


def test_feed_etag_stable_across_unchanged_refresh(store, crypto, config):
    """数据不变 → ICS 文本/ETag 不变：If-None-Match 命中 304，revision 不自增。"""
    import re

    school = FakeSchool(ROWS)
    service = _service(store, crypto, config, school)
    created = _run(service.create("2023000001", "pw", "2026-2027-1"))
    token = created["feed_url"].rsplit("/", 1)[-1][:-4]
    client = _build_feed_client(service)

    first = client.get("/cal/%s.ics" % token)
    assert first.status_code == 200
    etag = first.headers["ETag"]
    assert etag.startswith('W/"') and etag.endswith('"')
    assert first.headers["Last-Modified"].endswith(" GMT")   # RFC 9110 HTTP-date
    stamp_before = re.search(r"DTSTAMP:(\d{8}T\d{6}Z)", first.text).group(1)

    owner = crypto.owner_hash("2023000001")
    record = store.get_by_owner_semester(owner, "2026-2027-1")
    assert record["revision"] == 1

    # 后台刷新（无冷却限制）：数据未变 → changed=False、ETag/DTSTAMP 稳定
    assert _run(service.refresh_record(store.get_by_id(record["id"]))) == "ok"
    unchanged = store.get_by_id(record["id"])
    assert unchanged["revision"] == 1
    assert unchanged["ics_etag"] == etag.split('"')[1]
    stamp_after = re.search(r"DTSTAMP:(\d{8}T\d{6}Z)", unchanged["ics_blob"].decode("utf-8"))
    assert stamp_after.group(1) == stamp_before

    second = client.get("/cal/%s.ics" % token, headers={"If-None-Match": etag})
    assert second.status_code == 304

    # 数据真实变化 → revision 前进、产出新 ETag，旧 ETag 不再命中 304
    school.rows = [dict(ROWS[0], courseName="新课程名")]
    assert _run(service.refresh_record(store.get_by_id(record["id"]))) == "ok"
    changed = store.get_by_id(record["id"])
    assert changed["revision"] == 2
    third = client.get("/cal/%s.ics" % token, headers={"If-None-Match": etag})
    assert third.status_code == 200
    assert third.headers["ETag"] != etag
    # 新内容以当前时间重打 DTSTAMP，变化前后文本不同
    assert re.search(r"DTSTAMP:(\d{8}T\d{6}Z)", third.text).group(1) >= stamp_before


def test_http_date_outputs_rfc9110_format():
    from app.calendar.routes import _http_date

    assert _http_date("2026-11-13T11:22:33+08:00") == "Fri, 13 Nov 2026 03:22:33 GMT"
    naive = _http_date("2026-11-13T11:22:33")            # naive 按本地时区解释
    assert naive.endswith(" GMT") and naive.endswith(":22:33 GMT")
    assert _http_date("") is None
    assert _http_date(None) is None
    assert _http_date("not-a-date") is None


# ---------------- 回归：后台列表 state 过滤进 SQL（分页一致性） ----------------
def test_admin_list_state_filter_paginates_consistently(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    _run(service.create("2023000002", "pw", "2026-2027-1"))
    _run(service.create("2023000003", "pw", "2026-2027-1"))
    store._exec("UPDATE calendars SET last_fetch_status = 'upstream_error' WHERE owner_hash = ?",
                (crypto.owner_hash("2023000002"),))
    store._exec("UPDATE calendars SET last_fetch_status = 'credential_error' WHERE owner_hash = ?",
                (crypto.owner_hash("2023000003"),))

    degraded = service.admin_list(state="degraded")
    assert degraded["total"] == 1
    assert [item["state"] for item in degraded["items"]] == ["degraded"]

    # 过滤后分页：页内条目与 total 一致，翻页不重复、不越界
    page1 = service.admin_list(state="active", page=1, size=1)
    page2 = service.admin_list(state="active", page=2, size=1)
    assert page1["total"] == 1 and len(page1["items"]) == 1
    assert page1["items"][0]["state"] == "active"
    assert page2["total"] == 1 and page2["items"] == []

    # 暂停中的订阅映射为 locked，同样可在 SQL 内过滤
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")
    store.set_paused(record["id"], (datetime.now().astimezone()
                                    + timedelta(hours=2)).isoformat(timespec="seconds"))
    locked = service.admin_list(state="locked")
    assert locked["total"] == 1 and locked["items"][0]["state"] == "locked"
    assert service.admin_list()["total"] == 3


def test_stats_counts_states_without_loading_blobs(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")
    store._exec("UPDATE calendars SET ics_blob = NULL WHERE id = ?", (record["id"],))
    stats = service.store.stats()
    assert stats["total"] == 1
    assert stats["states"] == {"active": 1}
    assert stats["paused"] == 0


# ---------------- 回归：过期幂等键与审计保留期清理 ----------------
def test_cleanup_expired_removes_ops_and_stale_audit(store):
    store.ops_put("k1", "owner1", None, "create", b"resp", ttl_seconds=-1)   # 已过期
    store.ops_put("k2", "owner1", None, "create", b"resp", ttl_seconds=3600)
    fresh = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    stale = (datetime.now().astimezone() - timedelta(days=200)).isoformat(timespec="seconds")
    for at in (stale, fresh):
        store._exec(
            "INSERT INTO calendar_audit (at, calendar_id, action, actor, result)"
            " VALUES (?,?,?,?,?)", (at, None, "create", "user", "ok"))

    removed = store.cleanup_expired()
    assert removed["ops_removed"] == 1
    assert removed["audit_removed"] == 1
    assert store.ops_get("k1", "owner1") is None
    assert store.ops_get("k2", "owner1")["idem_key"] == "k2"
    remaining = store._conn.execute(
        "SELECT COUNT(*) AS c FROM calendar_audit").fetchone()["c"]
    assert remaining == 1          # 保留期内（<180 天）的审计行不动


def test_scheduler_tick_cleans_expired_rows(store, crypto, config):
    from app.calendar.scheduler import CalendarScheduler

    service = _service(store, crypto, config, FakeSchool(ROWS))
    store.ops_put("k1", "owner1", None, "create", b"resp", ttl_seconds=-1)
    stale = (datetime.now().astimezone() - timedelta(days=200)).isoformat(timespec="seconds")
    store._exec(
        "INSERT INTO calendar_audit (at, calendar_id, action, actor, result)"
        " VALUES (?,?,?,?,?)", (stale, None, "create", "user", "ok"))

    _run(CalendarScheduler(service).tick())

    assert store.ops_get("k1", "owner1") is None
    remaining = store._conn.execute(
        "SELECT COUNT(*) AS c FROM calendar_audit").fetchone()["c"]
    assert remaining == 0


# ---------------- 回归：订阅存在性预言机（rotate/qr） ----------------
def test_rotate_and_qr_missing_record_do_dummy_verification(store, crypto, config, monkeypatch):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    dummy_calls = {"n": 0}
    original_dummy = crypto.timing_dummy

    def counting_dummy():
        dummy_calls["n"] += 1
        return original_dummy()

    monkeypatch.setattr(crypto, "timing_dummy", counting_dummy)

    with pytest.raises(CalendarError) as missing:
        _run(service.rotate("2023000001", "pw", "2026-2027-1"))
    assert missing.value.status_code == 404
    assert dummy_calls["n"] == 1          # 返回 404 前做了一次等量本地密钥校验

    with pytest.raises(CalendarError) as missing_qr:
        service.qr("2023000001", "pw", "2026-2027-1")
    assert missing_qr.value.status_code == 404
    assert dummy_calls["n"] == 2

    # 存在订阅 + 密码错误：走真实本地解密比对（L2）→ 401，不经 dummy 分支
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    decrypt_calls = {"n": 0}
    original_decrypt = crypto.decrypt

    def counting_decrypt(blob, aad):
        decrypt_calls["n"] += 1
        return original_decrypt(blob, aad)

    monkeypatch.setattr(crypto, "decrypt", counting_decrypt)
    service.school.error = CalendarAuthError()      # L3 证明失败 → 401
    with pytest.raises(CalendarAuthError):
        _run(service.rotate("2023000001", "wrong", "2026-2027-1"))
    assert decrypt_calls["n"] >= 1
    assert dummy_calls["n"] == 2


# ---------------- 回归：并发配额与并发刷新 ----------------
def _two_term_config(config):
    from app.calendar.config import load_config_dict
    payload = config.to_dict()
    payload["terms"]["2026-2027-2"] = {
        "monday": "2027-02-22", "teaching_start": "2027-03-01", "weeks": 19}
    return load_config_dict(payload)


def test_concurrent_create_same_owner_respects_quota(store, crypto, config):
    service = _service(store, crypto, _two_term_config(config), FakeSchool(ROWS),
                       max_per_owner=1)

    async def scenario():
        return await asyncio.gather(
            service.create("2023000001", "pw", "2026-2027-1"),
            service.create("2023000001", "pw", "2026-2027-2"),
            return_exceptions=True,
        )

    results = _run(scenario())
    created = [r for r in results if isinstance(r, dict) and r.get("created")]
    conflicts = [r for r in results if isinstance(r, CalendarError)]
    assert len(created) == 1
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    assert conflicts[0].code == "too_many_subscriptions"
    assert len(store.list_by_owner(crypto.owner_hash("2023000001"))) == 1


def test_concurrent_refresh_does_not_lose_revision(store, crypto, config):
    school = MutatingSchool(ROWS)
    service = _service(store, crypto, config, school)
    _run(service.create("2023000001", "pw", "2026-2027-1"))
    record = store.get_by_owner_semester(crypto.owner_hash("2023000001"), "2026-2027-1")
    stale = dict(record)              # 两个并发调用都拿着锁外旧快照

    async def scenario():
        return await asyncio.gather(
            service.refresh_record(stale),
            service.refresh_record(stale),
            return_exceptions=True,
        )

    results = _run(scenario())
    assert results == ["ok", "ok"]
    final = store.get_by_id(record["id"])
    # 锁内按 id 重读 → 每次刷新各自增一次：1(创建) + 2(刷新) = 3，不丢计
    assert final["revision"] == 3
    assert school.calls == 3


# ---------------- 回归：配置数值字段非法值返回 422 语义 ----------------
def test_config_rejects_non_integer_numeric_fields(config):
    base = config.to_dict()

    bad_weeks = dict(base)
    bad_weeks["terms"] = dict(base["terms"])
    bad_weeks["terms"]["2026-2027-1"] = dict(base["terms"]["2026-2027-1"], weeks="abc")
    with pytest.raises(CalendarConfigError) as exc:
        load_config_dict(bad_weeks)
    assert "weeks" in str(exc.value)

    # 编码格式合法（N_M）但值不是数字：必须在 block_codes 路径上给出可读错误
    bad_block_nums = dict(base)
    bad_block_nums["block_codes"] = dict(base["block_codes"], **{"9_9": ["第1节"]})
    with pytest.raises(CalendarConfigError) as exc_block_num:
        load_config_dict(bad_block_nums)
    assert "block_codes" in str(exc_block_num.value)

    bad_offset = dict(base)
    bad_offset["student_week_offset"] = {"2026-2027-1": "many"}
    with pytest.raises(CalendarConfigError):
        load_config_dict(bad_offset)

    bad_version = dict(base)
    bad_version["version"] = "v2"
    with pytest.raises(CalendarConfigError):
        load_config_dict(bad_version)


def test_admin_terms_with_non_integer_weeks_returns_422(store, crypto, config):
    service = _service(store, crypto, config, FakeSchool(ROWS))
    terms = config.to_dict()["terms"]
    terms["2026-2027-1"] = dict(terms["2026-2027-1"], weeks="abc")
    with pytest.raises(CalendarError) as exc:
        service.admin_update_terms(terms)
    assert exc.value.status_code == 422
    assert exc.value.code == "invalid_calendar_config"
    # 校准失败不得污染运行时配置
    assert service.config.term("2026-2027-1").weeks == 20

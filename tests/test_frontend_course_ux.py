"""课表课程色块 / 课程详情浮层 / 成绩统计增强 / 周次导航 / 学号记忆的前端契约。

对应 2026-11-19 前端深度优化专项（参考 WakeUp 课程表式课表交互与
成绩/GPA 助手类工具的统计能力）：
- 课表 chip 仅对「节次×星期」签名网格渲染（includeAll 概况表不误染）；
- 统计只认已发布数值（与 gradeOverviewFacts 原语义一致），学分列缺失时诚实降级；
- 周导航仅单值周次 + 密码在表单内时显示，步进越界返回 null；
- 纯函数经 node 执行同一份实现（与 test_calendar_ui 的抽取模式一致）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _extract_function(script: str, signature: str) -> str:
    """按大括号配平从 app.js 抽出整个函数（node 中执行同一份实现）。"""
    start = script.index("function " + signature)
    brace = script.index("{", start)
    depth = 0
    for index in range(brace, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start:index + 1]
    raise AssertionError("函数未闭合: " + signature)


# ---------- 静态契约 ----------

def test_result_page_hosts_new_containers_and_singletons():
    page = _read("frontend/static/index.html")
    # 分布图容器紧跟概览条（先概览后详情）
    overview = page.index('id="resultOverview"')
    dist = page.index('id="gradeDistribution"')
    assert dist > overview
    assert 'class="grade-distribution hidden"' in page
    # 周导航位于结果头部与概览之间；浮层为单例容器（内容 JS 填充）
    head = page.index('class="result-head"')
    week = page.index('id="weekNav"')
    assert head < week < overview
    assert 'id="coursePopover"' in page and 'role="dialog"' in page
    assert 'aria-modal="false"' in page
    for element in ("coursePopoverTitle", "coursePopoverBody",
                    "coursePopoverCopy", "coursePopoverClose"):
        assert 'id="%s"' % element in page, element


def test_schedule_chip_marks_only_signature_grids():
    script = _read("frontend/static/app.js")
    # 签名门控：无成绩列 + 首列表头含「节次」才上色块
    assert 'indexOf("节次") !== -1' in script
    assert "function renderScheduleBodyCell(" in script
    assert 'class="course-chip course-accent-' in script
    # 浮层触发走事件委托，结果区重渲染不丢事件
    assert '.closest(".course-chip")' in script
    assert "function openCoursePopover(" in script
    assert "aria-expanded" in script and 'aria-haspopup="dialog"' in script


def test_week_nav_and_distribution_guards():
    script = _read("frontend/static/app.js")
    # 周导航：单值周次 + 密码仍在表单 + 步进越界隐藏
    assert "function weekNavStep(" in script
    assert "passwordInput.value.length > 0" in script
    assert 'dispatchEvent(new Event("submit"' in script
    # 分布图：<2 门不渲染（单门无分布意义）
    assert "stats.published < 2" in script
    assert 'role="img"' in script and "分数分布：" in script
    # 学号记忆：只填学号不碰密码，帮助文案如实标注来源
    assert "function autofillLastUsername(" in script
    assert "已自动填入上次查询的学号" in script
    assert "passwordInput.value = " not in script.split("autofillLastUsername", 1)[1][:600]


def test_styles_ship_accent_tokens_and_isnow_layering():
    css = _read("frontend/static/style.css")
    for accent in range(6):
        assert ".course-accent-%d" % accent in css, accent
    # 进行中节次 × 色块：主色 inset 环保证 is-now 在 chip 实底上仍可见
    assert "tr.is-now td .course-chip" in css
    assert ".course-popover" in css and ".grade-dist-fill" in css
    assert ".week-nav-btn" in css or ".week-nav" in css


# ---------- node 纯函数 ----------

GRADES_MARKDOWN = (
    "| 学期 | 课程 | 成绩 | GPA | 学分 | 课程类别 |\n"
    "|---|---|---|---|---|---|\n"
    "| 2025-2026-1 | 高等数学 | 92 | 4.0 | 4 | 必修 |\n"
    "| 2025-2026-1 | 大学英语 | 84 | 3.5 | 3 | 必修 |\n"
    "| 2025-2026-1 | 体育 | 58 | 0 | 1 | 实践 |\n"
    "| 2025-2026-1 | 专业导论 | -- | -- | 2 | 必修 |\n"
)

GRADES_NO_CREDIT = (
    "| 学期 | 课程 | 成绩 |\n"
    "|---|---|---|\n"
    "| 2025-2026-1 | 高等数学 | 90 |\n"
    "| 2025-2026-1 | 大学英语 | 80 |\n"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能执行前端片段")
def test_course_parsing_and_accent_pure_functions(tmp_path):
    script = _read("frontend/static/app.js")
    accent = _extract_function(script, "courseAccentIndex(name)")
    segments = _extract_function(script, "splitCourseSegments(rawCell)")
    parse = _extract_function(script, "parseCourseCell(segment)")
    harness = tmp_path / "course.mjs"
    harness.write_text(
        accent + "\n" + segments + "\n" + parse + "\n"
        "const assert = (ok, msg) => { if (!ok) { console.error(msg); process.exit(1); } };\n"
        "// courseAccentIndex：稳定、界内、空名回退\n"
        "const c1 = courseAccentIndex('高等数学');\n"
        "assert(c1 === courseAccentIndex('高等数学'), '同名恒定同色');\n"
        "assert(c1 >= 0 && c1 <= 5, '索引在 0..5, got ' + c1);\n"
        "assert(courseAccentIndex('') === 0 && courseAccentIndex(null) === 0, '空名回退');\n"
        "const spread = new Set(['高等数学','大学英语','体育','数据结构','毛概','物理','化学','音乐'].map(courseAccentIndex));\n"
        "assert(spread.size >= 3, '哈希分布足够散, got ' + spread.size);\n"
        "// splitCourseSegments：B 格式多课按「） / 」拆；不误切周次内嵌斜杠\n"
        "const multi = splitCourseSegments('高数（张三｜A101｜1-16周） / 大英（李四｜B202｜1-8周）');\n"
        "assert(multi.length === 2, '多课拆两段, got ' + multi.length);\n"
        "assert(multi[1] === '大英（李四｜B202｜1-8周）', '末段保留右括号');\n"
        "const inner = splitCourseSegments('体育（王五｜操场｜1-3/5-8周）');\n"
        "assert(inner.length === 1, '周次内嵌斜杠不拆');\n"
        "assert(splitCourseSegments('高数<br>张三<br>A101<br>1-16周').length === 1, 'A 格式整格一段');\n"
        "// parseCourseCell：B 格式字段顺序 = 教师｜教室｜周次\n"
        "const b = parseCourseCell('高等数学（张三｜A101｜1-16周）');\n"
        "assert(b && b.name === '高等数学' && b.teacher === '张三' && b.room === 'A101' && b.weeks === '1-16周', 'B 格式字段');\n"
        "// A 格式：<br> 分行，按语义归类；周次优先、教室含室/楼、其余归教师\n"
        "const a = parseCourseCell('大学英语<br>李四<br>B202<br>1-8周(单)');\n"
        "assert(a && a.name === '大学英语' && a.weeks === '1-8周(单)', 'A 格式周次归类');\n"
        "assert(a.room === 'B202', '教室归类, got ' + (a && a.room));\n"
        "assert(a.teacher === '李四', '教师归类, got ' + (a && a.teacher));\n"
        "// 归类不了的行进 extras（浮层如实展示，不冒充字段）\n"
        "const extra = parseCourseCell('体育<br>王五<br>操场<br>1-16周<br>需要自带球鞋');\n"
        "assert(extra && extra.extras.length === 1 && extra.extras[0] === '需要自带球鞋', 'extras 兜底');\n"
        "assert(parseCourseCell('') === null, '空段返回 null');\n"
        "console.log('ok');\n",
        encoding="utf-8")
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能执行前端片段")
def test_grade_stats_and_distribution_pure_functions(tmp_path):
    script = _read("frontend/static/app.js")
    separator = _extract_function(script, "isTableSeparator(line)")
    parse_table = _extract_function(script, "parseResultTable(text)")
    numeric = _extract_function(script, "numericScoreValue(raw)")
    stats_fn = _extract_function(script, "gradeStatsFromTable(table)")
    buckets = _extract_function(script, "scoreDistributionBuckets(scores)")
    week_step = _extract_function(script, "weekNavStep(currentWeek, maxWeeks, delta)")
    max_term_weeks = _extract_function(script, "maxTermWeeks(terms)")
    harness = tmp_path / "stats.mjs"
    harness.write_text(
        separator + "\n" + parse_table + "\n" + numeric + "\n" + stats_fn + "\n"
        + buckets + "\n" + week_step + "\n" + max_term_weeks + "\n"
        "const assert = (ok, msg) => { if (!ok) { console.error(msg); process.exit(1); } };\n"
        "// numericScoreValue：只认已发布数值\n"
        "assert(numericScoreValue('92') === 92, '数值');\n"
        "assert(numericScoreValue('--') === null && numericScoreValue('未发布') === null, '未发布回退');\n"
        "// 加权统计：92*4 + 84*3 + 58*1，学分基 = 8；加权 GPA = (4*4+3.5*3+0*1)/8\n"
        "const stats = gradeStatsFromTable(parseResultTable(" + repr(GRADES_MARKDOWN) + "));\n"
        "assert(stats.published === 3, '已发布 3 门(不含 --), got ' + stats.published);\n"
        "assert(Math.abs(stats.weightedAvg - 84.75) < 0.01, '加权平均 84.75, got ' + stats.weightedAvg);\n"
        "assert(Math.abs(stats.weightedGpa - 3.3125) < 0.001, '加权 GPA, got ' + stats.weightedGpa);\n"
        "assert(stats.credits === 10, '修读学分含未出分行 2 分 = 10, got ' + stats.credits);\n"
        "assert(stats.failed === 1, '不及格 1 门');\n"
        "assert(stats.maxScore === 92, '最高分');\n"
        "// 无学分列：加权项不出现，回退算术平均（诚实降级）\n"
        "const plain = gradeStatsFromTable(parseResultTable(" + repr(GRADES_NO_CREDIT) + "));\n"
        "assert(plain.weightedAvg === null && Math.abs(plain.simpleAvg - 85) < 0.01, '无学分列回退算术平均');\n"
        "assert(plain.weightedGpa === null && plain.credits === null, '无 GPA/学分列不出对应项');\n"
        "// 分布：五档边界 60/70/80/90\n"
        "const dist = scoreDistributionBuckets([92, 84, 58, 90, 80, 70, 60, 59.5]);\n"
        "assert(dist[0].count === 2 && dist[1].count === 2 && dist[2].count === 1 && dist[3].count === 1 && dist[4].count === 2, '五档计数');\n"
        "assert(scoreDistributionBuckets([]).every(b => b.count === 0), '空输入全零');\n"
        "// 周次步进：越界回 null（调用方隐藏对应方向）\n"
        "assert(weekNavStep(3, 20, 1) === 4 && weekNavStep(3, 20, -1) === 2, '正常步进');\n"
        "assert(weekNavStep(1, 20, -1) === null, '第 1 周无上一周');\n"
        "assert(weekNavStep(20, 20, 1) === null, '封顶周无下一周');\n"
        "assert(weekNavStep('all', 20, 1) === null, '非单值周次不步进');\n"
        "assert(weekNavStep(16, 16, 1) === null, '学期周数封顶');\n"
        "// maxTermWeeks：terms 为按学期键的对象（真实 /calendars/config 形状，\n"
        "// 曾误当数组致课表结果整段渲染抛 TypeError——此 fixture 钉死形状契约）\n"
        "const realTerms = { '2026-2027-1': { monday: '2026-08-31', teaching_start: '2026-09-01', weeks: 20 } };\n"
        "assert(maxTermWeeks(realTerms) === 20, '真实对象形状取最大周数');\n"
        "assert(maxTermWeeks({ a: { weeks: 16 }, b: { weeks: 20 } }) === 20, '多学期取最大');\n"
        "assert(maxTermWeeks([{ weeks: 16 }]) === 16, '容忍数组形状');\n"
        "assert(maxTermWeeks({ a: { monday: '2026-08-31' }, b: { weeks: 'abc' }, c: null, d: 7 }) === 0, '缺 weeks/坏项回 0');\n"
        "assert(maxTermWeeks(null) === 0 && maxTermWeeks(undefined) === 0, '空输入回 0');\n"
        "console.log('ok');\n",
        encoding="utf-8")
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout

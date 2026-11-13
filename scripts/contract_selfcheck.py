#!/usr/bin/env python3
"""学校侧契约自检：探测四层链路，逐条判定契约事实是否仍然成立。

用途：学校侧升级（换域名/换认证产品/改接口/轮换密钥）后，先跑本脚本定位
需要更新的契约条目，再按 docs/upstream-contract.md 的应急流程改
app/school_contract.py 一处即可恢复服务。

用法：
  # 无凭据快速自检（不登录，仅只读 GET；探测 WebVPN/门户/CAS 三层与公钥提取）
  python scripts/contract_selfcheck.py
  python scripts/contract_selfcheck.py --json

  # 深度自检（真实登录链路验证教务层；凭据经环境变量传入，不落盘、不打印）
  JWXT_HEALTH_USERNAME=2023000001 JWXT_HEALTH_PASSWORD=... \
      python scripts/contract_selfcheck.py --deep

  # 容器内执行（与生产同环境）：
  docker compose exec app python scripts/contract_selfcheck.py --json

退出码：0 = 未发现漂移（全部通过，或因不可达/未启用而无法判定）；
1 = 发现漂移证据（有契约事实判定为 False）；2 = 用法错误（如 --deep 缺凭据）。
"""

import argparse
import json as _json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jwxt_state import deep_check, probe_layers  # noqa: E402
from app.school_contract import SCHOOL_CONTRACT_VERSION  # noqa: E402

_MARK = {True: "[通过]", False: "[漂移]", None: "[未判定]"}
_LAYER_MARK = {True: "✓", False: "✗", None: "?"}


def _text_report(report, deep=None):
    """人类可读输出：分层结论 + 契约事实逐条判定 + （可选）真实登录链路。"""
    lines = ["学校侧契约自检  contract_version=%s" % SCHOOL_CONTRACT_VERSION, ""]
    lines.append("== 分层结论 ==")
    layers = report.get("layers") or {}
    for name in ("webvpn", "cas", "portal"):
        info = layers.get(name) or {}
        ok = info.get("ok")
        note = info.get("error") or ("HTTP %s" % info["status"] if info.get("status") else "")
        lines.append("  %s %-8s %6sms  %s" % (
            _LAYER_MARK.get(ok, "?"), name, info.get("latency_ms", "-"), note))
    if report.get("error"):
        lines.append("  探测整体异常：%s" % report["error"])

    checks = report.get("checks") or []
    lines.append("")
    lines.append("== 契约事实（%d 项）==" % len(checks))
    for c in checks:
        mark = _MARK.get(c.get("ok"), "[?]")
        note = c.get("detail") or c.get("error") or ""
        lines.append("  %s %-24s %-8s %s" % (mark, c.get("name", "?"), c.get("layer", "-"), note))

    if deep is not None:
        lines.append("")
        lines.append("== 真实登录链路（deep）==")
        for s in deep.get("stages") or []:
            mark = _LAYER_MARK.get(s.get("ok"), "?")
            extra = ""
            if s.get("error"):
                extra = "  %s" % s["error"]
            lines.append("  %s %-20s %-12s %6sms%s" % (
                mark, s.get("stage", "?"), "layer=%s" % s["layer"] if s.get("layer") else "-",
                s.get("at_ms", "-"), extra))
        layers_jwxt = (deep.get("layers") or {}).get("jwxt") or {}
        lines.append("  教务层结论：%s（semester=%s）" % (
            _MARK.get(layers_jwxt.get("ok")), deep.get("semester") or "-"))
        if deep.get("error"):
            lines.append("  错误：%s" % deep["error"])

    failed = [c for c in checks if c.get("ok") is False]
    lines.append("")
    if failed or (deep is not None and deep.get("ok") is False
                  and (deep.get("layers") or {}).get("jwxt", {}).get("ok") is False):
        lines.append("结论：发现 %d 条漂移证据 —— 请按 docs/upstream-contract.md 应急流程"
                     "更新 app/school_contract.py 对应条目" % len(failed))
    else:
        lines.append("结论：未发现漂移证据（不可达/未判定项不代表通过，建议稍后重跑）")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="学校侧契约自检（无凭据分层探测 + 可选真实登录链路）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    ap.add_argument("--deep", action="store_true",
                    help="附加真实登录链路探测（需要 JWXT_HEALTH_USERNAME/PASSWORD 或 --username/--password）")
    ap.add_argument("--timeout", type=int, default=6, help="单请求超时秒数（默认 6）")
    ap.add_argument("--username", default="", help="学号（--deep 时；默认取 JWXT_HEALTH_USERNAME）")
    ap.add_argument("--password", default="", help="密码（--deep 时；默认取 JWXT_HEALTH_PASSWORD，不落盘不打印）")
    args = ap.parse_args(argv)

    report = probe_layers(timeout=args.timeout)
    deep = None
    if args.deep:
        username = args.username or os.environ.get("JWXT_HEALTH_USERNAME", "")
        password = args.password or os.environ.get("JWXT_HEALTH_PASSWORD", "")
        if not username or not password:
            print("缺少凭据：请提供 --username/--password 或环境变量 "
                  "JWXT_HEALTH_USERNAME/JWXT_HEALTH_PASSWORD", file=sys.stderr)
            return 2
        deep = deep_check(username, password)

    if args.json:
        payload = {"contract_version": SCHOOL_CONTRACT_VERSION, "probe": report}
        if deep is not None:
            payload["deep"] = {
                "ok": deep.get("ok"),
                "semester": deep.get("semester"),
                "layers": deep.get("layers") or {},
                "stages": deep.get("stages") or [],
                "error": deep.get("error"),
                "contract_version": deep.get("contract_version"),
            }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_text_report(report, deep))

    drift = [c for c in (report.get("checks") or []) if c.get("ok") is False]
    if deep is not None and deep.get("ok") is False \
            and (deep.get("layers") or {}).get("jwxt", {}).get("ok") is False:
        drift.append({"name": "deep.jwxt_chain"})
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())

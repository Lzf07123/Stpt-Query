"""前端内联脚本 CSP 哈希与重试交互回归测试。"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _inline_scripts(filename: str) -> list[str]:
    html = (ROOT / "frontend" / "static" / filename).read_text(encoding="utf-8")
    return [match.group(1) for match in re.finditer(r"<script>(.*?)</script>", html, re.DOTALL)]


def test_frontend_inline_scripts_are_allowed_by_csp():
    policy = (ROOT / "frontend" / "templates" / "default.conf.template").read_text(
        encoding="utf-8"
    )
    for filename in ("index.html", "admin.html"):
        scripts = _inline_scripts(filename)
        assert scripts, filename
        for script in scripts:
            digest = hashlib.sha256(script.encode("utf-8")).digest()
            expected = "sha256-" + base64.b64encode(digest).decode("ascii")
            assert f"'{expected}'" in policy


def test_retry_buttons_use_delegated_listener_not_inline_handler():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="empty-state-action btn btn-primary js-retry-query"' in index
    assert 'class="btn btn-ghost js-retry-query"' in index
    assert "onclick=" not in index
    assert '.closest(".js-retry-query")' in index

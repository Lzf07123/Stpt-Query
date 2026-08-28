"""pytest 公共配置：把 format-service 目录加入导入路径（其内为 app 包）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "format-service"
QUERY_PROXY = ROOT / "get-infomation-service"
for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)
if str(QUERY_PROXY) not in sys.path:
    sys.path.append(str(QUERY_PROXY))

"""订阅二维码（本地生成，不经过任何第三方服务）。

设计约束：
- 订阅地址是长期密钥：二维码必须在本进程生成，前端不上传地址，也不允许调用第三方
  二维码 / 短链服务，否则等于把订阅密钥交给外部；
- 只返回**模块矩阵**而不是位图：前端按设备像素比绘制，缩放不糊，服务端也不需要图像
  编码后端（镜像内的 reportlab 未安装 renderPM 光栅后端）；
- 复用已有 reportlab 依赖（``QrCodeWidget``），不新增第三方包；
- 纠错等级固定 M（约 15%），版本由编码器按内容长度自动选择。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from reportlab.graphics.barcode.qr import QrCodeWidget

ECC_LEVEL = "M"
QUIET_ZONE_MODULES = 4
MAX_PAYLOAD_BYTES = 1200


class QrPayloadError(ValueError):
    """二维码内容不可编码（为空、超长或不是 http(s) 地址）。"""


def webcal_url(feed_url: str) -> str:
    """http(s) 订阅地址 → webcal://，交给系统日历 App 处理（不经第三方服务器）。"""
    value = str(feed_url or "").strip()
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        raise QrPayloadError("订阅地址必须是 http(s) URL")
    return re.sub(r"^https?://", "webcal://", value, count=1, flags=re.IGNORECASE)


def qr_matrix(payload: str, ecc: str = ECC_LEVEL) -> Dict[str, Any]:
    """返回 "0/1" 行字符串矩阵，供前端 canvas 绘制。"""
    text = str(payload or "")
    if not text or len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise QrPayloadError("二维码内容长度不合法")
    widget = QrCodeWidget(text, barWidth=1, barHeight=1, barLevel=ecc)
    matrix = widget.qr
    matrix.make()
    size = int(matrix.moduleCount)
    rows: List[str] = [
        "".join("1" if matrix.isDark(row, col) else "0" for col in range(size))
        for row in range(size)
    ]
    return {
        "ecc": ecc,
        "version": int(matrix.version),
        "size": size,
        "quiet_zone": QUIET_ZONE_MODULES,
        "rows": rows,
    }

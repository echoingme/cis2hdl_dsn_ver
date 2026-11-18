"""页面尺寸解码器（P2-1 展示维度）。

页面头（实测 gold 口径）：``[8B DELIM][0d 00][页面名 zstr][00][02 00]
[41 33 00='A3'][?...][9c ff ff ff=-100][64 00 00 00=100]
[9c ff ff ff=-100][a0 68 06 00=420000][28 88 04 00=297000]``。

- 单位 **µm**（420000µm = 420mm = A3 长边）；换算 ``mil = µm/25.4``
  round()：420000/25.4=16535.43→**16535**、297000/25.4=11692.91→**11693**。
- **不得**将 420000 当 mil（旧实现硬编码 3520×2720 即错误）。
- gold 口径下 'A3' 后有额外字节（`f9 6f 74 68 1e 30 85 6a`），
  因此**按 420000/297000 数值模式扫描**而非固定偏移。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: A3 页面尺寸（µm）：420000×297000
_A3_WIDTH_UM = 420000
_A3_HEIGHT_UM = 297000
#: µm → mil 换算
_UM_TO_MIL = 25.4

#: 页面尺寸数值模式（a0 68 06 00 = 420000, 28 88 04 00 = 297000）
_WIDTH_BYTES = _A3_WIDTH_UM.to_bytes(4, "little")
_HEIGHT_BYTES = _A3_HEIGHT_UM.to_bytes(4, "little")


@dataclass
class PageSize:
    """页面尺寸解析结果。

    Attributes:
        spec: 页面规格（如 'A3'；未知为空）。
        width_um, height_um: 尺寸（µm）。
        width_mil, height_mil: 尺寸（mil，round(µm/25.4)）。
    """

    spec: str = ""
    width_um: int = 0
    height_um: int = 0
    width_mil: int = 0
    height_mil: int = 0

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "width_um": self.width_um,
            "height_um": self.height_um,
            "width_mil": self.width_mil,
            "height_mil": self.height_mil,
        }


def parse_page_size(page_data: bytes) -> PageSize:
    """从页面流解析页面尺寸（µm → mil）。

    Args:
        page_data: 页面流原始字节。

    Returns:
        PageSize；未找到 A3 尺寸时返回全 0（调用方保留默认 3520×2720）。
    """
    # 1. 页面规格（'A3'）
    spec = ""
    m = re.search(rb"[A-Za-z0-9]{1,4}\x00", page_data[:256])
    for cand in re.finditer(rb"([A-Z][0-9]?)\x00", page_data[:256]):
        s = cand.group(1).decode("latin1")
        if s in ("A3", "A4", "A2", "A1", "A0", "B1", "B2", "B3", "B4"):
            spec = s
            break
    # 2. 数值模式扫描（防御 gold 额外字节 / 偏移差异）
    #    从帧内找到的偏移处**直接读取 u32 实际值**（非硬编码常量赋值），
    #    支持任意页面尺寸（A3=420000×297000µm、A4=297000×210000µm 等）。
    width_um = 0
    height_um = 0
    wi = page_data.find(_WIDTH_BYTES)
    hi = page_data.find(_HEIGHT_BYTES)
    if wi != -1 and 0 < wi < 512:
        width_um = int.from_bytes(page_data[wi:wi + 4], "little")
    if hi != -1 and 0 < hi < 512:
        height_um = int.from_bytes(page_data[hi:hi + 4], "little")
    if not width_um and not height_um:
        # 兜底：扫描 4B 小端值（前 512B），读任意合法尺寸
        for off in range(0, min(len(page_data) - 4, 512)):
            v = int.from_bytes(page_data[off:off + 4], "little")
            if not width_um and v == _A3_WIDTH_UM:
                width_um = v
            elif not height_um and v == _A3_HEIGHT_UM:
                height_um = v
    if not width_um or not height_um:
        return PageSize(spec=spec)
    return PageSize(
        spec=spec,
        width_um=width_um,
        height_um=height_um,
        width_mil=round(width_um / _UM_TO_MIL),
        height_mil=round(height_um / _UM_TO_MIL),
    )

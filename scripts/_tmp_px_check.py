# 临时脚本：对比修改前后截图中下拉面板区域的颜色（验证后删除）
import sys
from pathlib import Path

from PIL import Image

BASE = Path(__file__).resolve().parent.parent / "data" / "logs"
BEFORE = BASE / "ui-verify-dropdown-white.png"
AFTER = BASE / "ui-verify-dropdown-dark.png"


def analyze(path):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    white = 0       # 近白像素
    dark = 0        # 深色像素
    white_box = None
    rows = {}
    for y in range(h):
        row_white = 0
        for x in range(w):
            r, g, b = px[x, y]
            if r > 235 and g > 235 and b > 235:
                white += 1
                row_white += 1
                if white_box is None:
                    white_box = [x, y, x, y]
                white_box[0] = min(white_box[0], x)
                white_box[1] = min(white_box[1], y)
                white_box[2] = max(white_box[2], x)
                white_box[3] = max(white_box[3], y)
            elif r < 80 and g < 80 and b < 120:
                dark += 1
        rows[y] = row_white
    return {
        "size": f"{w}x{h}",
        "white_px": white,
        "dark_px": dark,
        "white_ratio": round(white / (w * h), 4),
        "white_bbox": white_box,
        "white_rows": sorted([y for y, c in rows.items() if c > 50])[:10],
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "both"
    if target in ("before", "both"):
        print("BEFORE:", analyze(BEFORE))
    if target in ("after", "both"):
        print("AFTER:", analyze(AFTER))

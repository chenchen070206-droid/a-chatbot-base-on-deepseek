# -*- coding: utf-8 -*-
"""把动图抽帧拼成一张联系表，便于视觉模型一次看完。

用法：python gif_sheet.py <动图> [输出png] [帧数]
（被 import 时不会执行任何东西，可以直接 import 使用 make_sheet()）
"""
import sys
import os
from PIL import Image, ImageSequence


def make_sheet(src, out=None, n=6):
    """抽 n 帧拼成 3 列的联系表，返回输出路径。"""
    out = out or os.path.splitext(src)[0] + "_sheet.png"
    im = Image.open(src)
    frames = []
    for f in ImageSequence.Iterator(im):
        frames.append(f.convert("RGB").copy())
    total = len(frames)
    print("frames:", total, "size:", im.size,
          "info:", {k: im.info.get(k) for k in ("duration", "loop")})
    if not frames:
        raise SystemExit("没有帧")
    picks = [frames[min(total - 1, round(i * (total - 1) / max(1, n - 1)))] for i in range(n)]
    w, h = picks[0].size
    if w > 400:
        scale = 400 / w
        w, h = int(w * scale), int(h * scale)
    picks = [p.resize((w, h)) for p in picks]
    cols = 3
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), "white")
    for i, p in enumerate(picks):
        sheet.paste(p, ((i % cols) * w, (i // cols) * h))
    sheet.save(out)
    print("saved:", out, sheet.size)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法: python gif_sheet.py <动图> [输出png] [帧数]")
        return 1
    src = argv[0]
    out = argv[1] if len(argv) > 1 else None
    n = int(argv[2]) if len(argv) > 2 else 6
    make_sheet(src, out, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

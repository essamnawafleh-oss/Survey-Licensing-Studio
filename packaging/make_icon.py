"""Generate packaging/sls.ico (application and installer icon) — a parcel outline with its setback line.

Run: python packaging/make_icon.py   (deterministic; the result is committed)
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
OUT = Path(__file__).with_name("sls.ico")


def draw(size: int = SIZE) -> Image.Image:
    s = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=44 * s, fill=(21, 101, 192, 255))
    parcel = [(48, 196), (60, 56), (196, 44), (212, 188)]
    d.line([(x * s, y * s) for x, y in parcel + parcel[:1]], fill=(255, 255, 255, 255), width=max(1, int(14 * s)),
           joint="curve")
    inner = [(84, 168), (90, 86), (172, 78), (182, 164)]
    pts = [(x * s, y * s) for x, y in inner + inner[:1]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):  # dashed setback line
        n = 6
        for k in range(0, n, 2):
            a, b = k / n, (k + 1) / n
            d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                   fill=(255, 213, 79, 255), width=max(1, int(9 * s)))
    return im


if __name__ == "__main__":
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(OUT)

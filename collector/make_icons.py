"""Draws the home screen icon (a plain 3D printer outline on blue).
Run once: python collector/make_icons.py  (needs Pillow)."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "docs" / "icons"
OUT.mkdir(parents=True, exist_ok=True)
S = 1024
BLUE, WHITE = (11, 99, 206), (255, 255, 255)

img = Image.new("RGB", (S, S), BLUE)
d = ImageDraw.Draw(img)
W = 44                                                    # stroke width
# frame
d.rounded_rectangle((212, 200, 812, 800), radius=56, outline=WHITE, width=W)
# gantry bar
d.rounded_rectangle((212, 352, 812, 352 + W), radius=10, fill=WHITE)
# print head and nozzle
d.rounded_rectangle((442, 310, 582, 440), radius=22, fill=WHITE)
d.polygon([(472, 440), (552, 440), (512, 500)], fill=WHITE)
# printed part (stacked layers) on the bed
for i, (x0, x1) in enumerate([(402, 622), (402, 622), (432, 592)]):
    y = 690 - i * 58
    d.rounded_rectangle((x0, y, x1, y + 40), radius=12, fill=WHITE)
# feet / bed line
d.rounded_rectangle((292, 742, 732, 742 + 18), radius=9, fill=WHITE)

for size in (180, 192, 512):
    img.resize((size, size), Image.LANCZOS).save(OUT / f"icon-{size}.png", optimize=True)
print("icons written to", OUT)

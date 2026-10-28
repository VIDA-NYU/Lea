#!/usr/bin/env python3
"""Build assets/icon.svg, icon.png (1024px) and icon.icns from the Lea wordmark.

macOS only (uses qlmanage, sips and iconutil). Re-run after changing the wordmark.
"""
import pathlib
import re
import shutil
import subprocess
import sys

APP = pathlib.Path(__file__).resolve().parent.parent
ROOT = APP.parent.parent
ASSETS = APP / "assets"
SOURCE = ROOT / "public/assets/img/lea-wordmark-light.svg"
SIZE = 1024
# Apple's template: the icon shape fills 824 of 1024 px, centred.
INSET, SHAPE, RADIUS = 100, 824, 185


def main() -> int:
    text = SOURCE.read_text()
    viewbox = re.search(r'viewBox="([^"]+)"', text).group(1)
    path_d = re.search(r'<path[^>]*\sd="([^"]+)"', text).group(1)
    vx, vy, vw, vh = (float(v) for v in viewbox.split())

    target_w = SHAPE * 0.74
    scale = target_w / vw
    tw, th = vw * scale, vh * scale
    tx, ty = (SIZE - tw) / 2, (SIZE - th) / 2

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2f5aa8"/>
      <stop offset=".38" stop-color="#128a8a"/>
      <stop offset=".72" stop-color="#16733d"/>
      <stop offset="1" stop-color="#0d4f2b"/>
    </linearGradient>
  </defs>
  <rect x="{INSET}" y="{INSET}" width="{SHAPE}" height="{SHAPE}" rx="{RADIUS}" fill="url(#bg)"/>
  <g transform="translate({tx:.2f} {ty:.2f}) scale({scale:.5f}) translate({-vx:g} {-vy:g})">
    <path fill="#ffffff" d="{path_d}"/>
  </g>
</svg>
"""
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "icon.svg").write_text(svg)

    png = ASSETS / "icon.png"
    subprocess.run(["qlmanage", "-t", "-s", str(SIZE), "-o", str(ASSETS), str(ASSETS / "icon.svg")],
                   check=True, capture_output=True)
    shutil.move(str(ASSETS / "icon.svg.png"), str(png))

    iconset = ASSETS / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale_factor, suffix in ((1, ""), (2, "@2x")):
            px = size * scale_factor
            subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out",
                            str(iconset / f"icon_{size}x{size}{suffix}.png")],
                           check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)
    shutil.rmtree(iconset)
    print(f"wrote {png} and {ASSETS / 'icon.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

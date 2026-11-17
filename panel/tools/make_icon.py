"""Make the panel's icon from the PicnicPlace logo: an LVGL image in C.

The logo is the color basket on a transparent background that the Studio's top bar uses
(docs/brand/picnicplace-icon-transparent.png), cropped and scaled to fit a square, kept in
32-bit color with its transparency. Run it again after changing the picture, from the
repository root, with the core's environment (it needs OpenCV):

    cd core && uv run python -I ../panel/tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "brand" / "picnicplace-icon-transparent.png"
OUTPUT = ROOT / "panel" / "firmware" / "main" / "icon.c"
SIZE = 40
"""Pixels square: it sits in the 56 px status bar."""


def main() -> None:
    bgra = cv2.imread(str(SOURCE), cv2.IMREAD_UNCHANGED)
    if bgra is None or bgra.ndim != 3 or bgra.shape[2] != 4:
        raise SystemExit(f"cannot read {SOURCE} as a picture with transparency")
    ys, xs = np.nonzero(bgra[:, :, 3] > 8)
    bgra = bgra[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.float32)
    side = max(bgra.shape[:2])
    square = np.zeros((side, side, 4), np.float32)
    top, left = (side - bgra.shape[0]) // 2, (side - bgra.shape[1]) // 2
    square[top : top + bgra.shape[0], left : left + bgra.shape[1]] = bgra
    # Scale with the color weighted by its opacity, so the edges do not darken.
    alpha = square[:, :, 3:4] / 255.0
    weighted = np.concatenate([square[:, :, :3] * alpha, alpha * 255.0], axis=2)
    small = cv2.resize(weighted, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
    a = small[:, :, 3:4]
    color = np.where(a > 0, small[:, :, :3] * 255.0 / np.maximum(a, 1e-6), 0.0)
    pixels = np.concatenate([color, a], axis=2).round().clip(0, 255).astype(np.uint8)
    values = [f"0x{v:02x}" for v in pixels.flatten()]  # blue, green, red, alpha
    body = ",\n    ".join(", ".join(values[i : i + 12]) for i in range(0, len(values), 12))
    OUTPUT.write_text(
        f"""/* The PicnicPlace logo, {SIZE} x {SIZE}, in 32-bit color with transparency. Generated
 * by panel/tools/make_icon.py from docs/brand/picnicplace-icon-transparent.png: do not
 * edit by hand.
 *
 * This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
 */
#include "lvgl.h"

static const uint8_t ICON_PIXELS[] = {{
    {body},
}};

const lv_image_dsc_t picnicplace_icon = {{
    .header = {{
        .magic = LV_IMAGE_HEADER_MAGIC,
        .cf = LV_COLOR_FORMAT_ARGB8888,
        .w = {SIZE},
        .h = {SIZE},
        .stride = {SIZE * 4},
    }},
    .data_size = sizeof ICON_PIXELS,
    .data = ICON_PIXELS,
}};
"""
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()

"""Trace the PicnicPlace logo into outlines a board script can draw in silkscreen.

KiCad's Python has no image library, so this runs with the core's environment (OpenCV),
from the repository root:

    cd core && uv run python -I ../hardware/logo/make_logo_outline.py

It reads picnicplace-icon-bw.png (black artwork on white) and writes
picnicplace-icon-outline.json next to it: the black areas as polygons, each an outline
with its holes, in units of the artwork's height (0 to 1, y down, cropped to the
artwork). Run it again after changing the picture.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "picnicplace-icon-bw.png"
OUTPUT = HERE / "picnicplace-icon-outline.json"
SIMPLIFY_PX = 1.0
"""How far a traced outline may stray from the picture's edge, in the picture's pixels."""
MIN_AREA_PX = 30.0
"""Specks smaller than this are dropped."""


def main() -> None:
    image = cv2.imread(str(SOURCE), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise SystemExit(f"cannot read {SOURCE}")
    ink = (image < 128).astype(np.uint8)
    ys, xs = np.nonzero(ink)
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    ink = np.ascontiguousarray(ink[y0:y1, x0:x1])
    height = float(y1 - y0)
    contours, hierarchy = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)

    def points(c: np.ndarray) -> list[list[float]]:
        c = cv2.approxPolyDP(c, SIMPLIFY_PX, closed=True)
        return [[round(float(p[0][0]) / height, 5), round(float(p[0][1]) / height, 5)] for p in c]

    polygons: list[dict[str, object]] = []
    for i, c in enumerate(contours):
        if hierarchy[0][i][3] != -1 or cv2.contourArea(c) < MIN_AREA_PX:
            continue  # a hole, taken with its outline below; or a speck
        holes: list[list[list[float]]] = []
        j = hierarchy[0][i][2]
        while j != -1:
            if cv2.contourArea(contours[j]) >= MIN_AREA_PX:
                holes.append(points(contours[j]))
            j = hierarchy[0][j][0]
        polygons.append({"outline": points(c), "holes": holes})
    out = {
        "source": SOURCE.name,
        "aspect": round((x1 - x0) / height, 5),
        "polygons": polygons,
    }
    OUTPUT.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"{OUTPUT}: {len(polygons)} outlines, {sum(len(p['holes']) for p in polygons)} holes")


if __name__ == "__main__":
    main()

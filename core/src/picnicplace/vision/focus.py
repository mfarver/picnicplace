"""How sharp part of an image is, for finding a camera's focus."""

import cv2
import numpy as np

Image = np.ndarray


def sharpness(image: Image, x: float, y: float, radius: float) -> float:
    """The variance of the Laplacian in a square around (x, y), in pixels: higher is
    sharper. Only compares frames of the same scene."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    r = max(4, round(radius))
    x0, x1 = max(0, round(x) - r), min(w, round(x) + r)
    y0, y1 = max(0, round(y) - r), min(h, round(y) + r)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return 0.0
    return float(cv2.Laplacian(gray[y0:y1, x0:x1], cv2.CV_64F).var())

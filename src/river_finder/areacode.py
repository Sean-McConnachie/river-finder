"""The 8-bit catchment-area code used by the viewer images and the national ``area_code.tif`` tiles.

0 = nodata (sea), 1 = land below ``CODE_MIN_KM2`` (smaller than one cell, so practically unused),
2..255 = log10(area km2) from ``CODE_MIN_KM2`` (2e-5 km2 = 20 m2, under one 5 m or 8 m cell) to
``CODE_MAX_KM2`` (30 000 km2), about 8.7 % per step. Every land cell therefore has an area: hillslopes
as well as channels.
"""

from __future__ import annotations

import math

import numpy as np

CODE_MIN_KM2 = 2e-5
CODE_MAX_KM2 = 30_000.0
CODE_LOG_MIN = math.log10(CODE_MIN_KM2)
CODE_STEP = (math.log10(CODE_MAX_KM2) - CODE_LOG_MIN) / 253.0
CODE_TAG = "0 nodata, 1 < 2e-5 km2, 2..255 log10(area km2) from 2e-5 to 30000 km2"


def encode_area(area_km2: np.ndarray) -> np.ndarray:
    """uint8 code for a float area raster (NaN or negative = nodata)."""
    a = np.nan_to_num(np.asarray(area_km2, dtype=float), nan=-1.0)
    code = np.zeros(a.shape, np.uint8)
    land = a >= 0
    code[land] = 1
    m = a >= CODE_MIN_KM2
    code[m] = np.clip(2 + np.round((np.log10(a[m]) - CODE_LOG_MIN) / CODE_STEP), 2, 255).astype(np.uint8)
    return code


def decode_area(code: np.ndarray) -> np.ndarray:
    """Area km2 at the centre of each code step (NaN for 0 and 1)."""
    c = np.asarray(code, dtype=float)
    return np.where(c >= 2, 10 ** (CODE_LOG_MIN + (c - 2) * CODE_STEP), np.nan)

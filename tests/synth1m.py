"""A synthetic 1 m DEM for the tile-job tests (tests/test_nz1m.py): a valley draining south to the sea, a tributary,
a 6 m fall, a hydro-flattened lake with a no-data interior, a land gap for the 8 m fallback, and the matching 8 m
DEM (2 m too high, no sea)."""

import numpy as np

X0, Y0 = 1_000_000, 5_000_000
CORE = (X0 + 400, Y0 + 400, X0 + 2400, Y0 + 2400)
BOX = (X0, Y0, X0 + 2800, Y0 + 2800)
FALL_Y = Y0 + 1500                       # the fall on the main valley (x = X0 + 1400)
LAKE = (X0 + 1250, Y0 + 1900, X0 + 1550, Y0 + 2100)    # flattened water ...
HOLE = (X0 + 1330, Y0 + 1960, X0 + 1470, Y0 + 2040)    # ... with a no-data interior
GAP = (X0 + 300, Y0 + 900, X0 + 700, Y0 + 1300)        # no LiDAR: 8 m fallback
SEA_Y = Y0 + 150                                         # the sea south of this


def terrain(seed: int = 0):
    rng = np.random.default_rng(seed)
    n = BOX[2] - BOX[0]
    y = BOX[3] - 0.5 - np.arange(n)[:, None]             # cell centres, rows from the top
    x = BOX[0] + 0.5 + np.arange(n)[None, :]
    z = 20.0 + 0.04 * (y - Y0) + 0.12 * np.abs(x - (X0 + 1400))
    # a tributary from the north-east joining the main valley at y = Y0 + 1000
    dx = x - (X0 + 1400)
    zt = 60.0 + 0.05 * dx + 0.12 * np.abs(y - (Y0 + 1000 + 0.5 * dx))
    z = np.where(dx > 0, np.minimum(z, zt), z)
    z = z + np.where(y > FALL_Y, 6.0, 0.0)               # the fall
    z = z + rng.normal(0, 0.02, z.shape)
    z = z.astype(np.float32)
    lake = (x >= LAKE[0]) & (x < LAKE[2]) & (y >= LAKE[1]) & (y < LAKE[3])
    level = float(np.percentile(z[lake], 30))
    z = np.where(lake, np.float32(level), z).astype(np.float32)
    truth = z.copy()
    hole = (x >= HOLE[0]) & (x < HOLE[2]) & (y >= HOLE[1]) & (y < HOLE[3])
    gap = (x >= GAP[0]) & (x < GAP[2]) & (y >= GAP[1]) & (y < GAP[3])
    sea = np.broadcast_to(y < SEA_Y, z.shape)
    z[hole | gap | sea] = np.nan
    # 8 m DEM: block mean of the truth, 2 m too high, no data over the sea
    t8 = truth.reshape(n // 8, 8, n // 8, 8).mean(axis=(1, 3)) + 2.0
    y8 = BOX[3] - 4.0 - 8.0 * np.arange(n // 8)[:, None]
    t8 = np.where(np.broadcast_to(y8 < SEA_Y, t8.shape), np.nan, t8).astype(np.float32)
    return z, truth, t8, level

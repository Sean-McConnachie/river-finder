"""Regular lat/lon grids of the rainfall products, and the hour index used by the buffers.

A :class:`GridSpec` is the centre of the first (north-west) cell, the step and the shape. Rows run
north to south when ``dlat`` is negative, as in both products. The two 0.1° products are offset by
half a cell, so every grid has its own catchment weight matrix:

- **GSMaP** (JAXA, hourly, 60N-60S): 3600 x 1200 cells; the first cell centre is at 0.05E, 59.95N, so
  cell centres sit on x.x5° and cell edges on x.x0° (JAXA "GSMaP data format description").
- **ECMWF IFS open data, 0.1°** (``ifs/0p1/oper``): 3600 x 1801 grid points from 90N to 90S and from
  180° eastwards; points on multiples of 0.1° (GRIB2 section 3, template 3.0: La1=90, Lo1=180,
  Di=Dj=0.1, checked on the 2026-11-18 00z run). Each point is treated as the centre of a 0.1° cell,
  so cell centres sit on x.x0° and cell edges on x.x5°.

The NZ grids (``config/rainfall.toml``) are windows of these global grids that cover every national
tile with a margin; :meth:`GridSpec.crop` cuts a window out of a global array.

Hours are whole UTC hours counted from the Unix epoch, labelled by the *end* of the hour: hour ``h``
holds the rain that fell in ``(h - 1, h]``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TOL = 1e-6  # degrees: alignment tolerance between grids


@dataclass(frozen=True)
class GridSpec:
    """A regular lat/lon grid: ``lon0``/``lat0`` is the centre of cell [0, 0] (row 0 = first row)."""

    name: str
    lon0: float
    lat0: float
    dlon: float
    dlat: float  # negative: rows run north to south
    nx: int
    ny: int

    def __post_init__(self):
        if not (self.dlon > 0 and self.dlat != 0):
            raise ValueError(f"grid {self.name}: dlon must be > 0 and dlat != 0")
        if not (self.nx > 0 and self.ny > 0):
            raise ValueError(f"grid {self.name}: nx and ny must be > 0")
        if not (-90 <= self.lat0 <= 90 and -90 <= self.lat0 + (self.ny - 1) * self.dlat <= 90):
            raise ValueError(f"grid {self.name}: latitudes outside -90..90")

    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)

    @property
    def size(self) -> int:
        return self.nx * self.ny

    @property
    def lons(self) -> np.ndarray:
        return self.lon0 + np.arange(self.nx) * self.dlon

    @property
    def lats(self) -> np.ndarray:
        return self.lat0 + np.arange(self.ny) * self.dlat

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Outer cell edges (west, south, east, north)."""
        lon_a, lon_b = self.lon0 - self.dlon / 2, self.lon0 + (self.nx - 0.5) * self.dlon
        lat_a, lat_b = self.lat0 - self.dlat / 2, self.lat0 + (self.ny - 0.5) * self.dlat
        return (lon_a, min(lat_a, lat_b), lon_b, max(lat_a, lat_b))

    def same_cells(self, other: GridSpec) -> bool:
        """Same geometry (the name may differ)."""
        return (self.nx, self.ny) == (other.nx, other.ny) and all(
            abs(a - b) < _TOL for a, b in ((self.lon0, other.lon0), (self.lat0, other.lat0),
                                           (self.dlon, other.dlon), (self.dlat, other.dlat)))

    def cell_rc(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        """Row and column (int64) of the cell holding each point; may be outside ``0..ny-1/nx-1``."""
        col = np.floor((np.asarray(lon, dtype=np.float64) - (self.lon0 - self.dlon / 2)) / self.dlon)
        row = np.floor((np.asarray(lat, dtype=np.float64) - (self.lat0 - self.dlat / 2)) / self.dlat)
        return row.astype(np.int64), col.astype(np.int64)

    def cell_index(self, lon, lat) -> np.ndarray:
        """Flat cell index ``row * nx + col`` (C order, as ``grid_mm.ravel()``); -1 outside the grid."""
        r, c = self.cell_rc(lon, lat)
        ok = (r >= 0) & (r < self.ny) & (c >= 0) & (c < self.nx)
        return np.where(ok, r * self.nx + c, -1)

    def offset_in(self, parent: GridSpec) -> tuple[int, int]:
        """(row, col) of this grid's cell [0, 0] in ``parent``; raises unless the cells line up exactly.

        Longitudes are compared modulo 360, so a window at 166E fits a parent that starts at -180.
        """
        if abs(self.dlon - parent.dlon) > _TOL or abs(self.dlat - parent.dlat) > _TOL:
            raise ValueError(f"grid {self.name}: step differs from {parent.name}")
        fc = ((self.lon0 - parent.lon0) % 360.0) / parent.dlon
        fr = (self.lat0 - parent.lat0) / parent.dlat
        c, r = round(fc), round(fr)
        if abs(fc - c) > 1e-4 or abs(fr - r) > 1e-4:
            raise ValueError(f"grid {self.name}: cell centres are not on the {parent.name} grid "
                             f"(offset {fc - c:+.3f} cols, {fr - r:+.3f} rows)")
        wraps = parent.nx * parent.dlon >= 360.0 - _TOL
        if r < 0 or r + self.ny > parent.ny or (not wraps and c + self.nx > parent.nx):
            raise ValueError(f"grid {self.name}: not inside {parent.name}")
        return r, c

    def crop(self, parent_values: np.ndarray, parent: GridSpec) -> np.ndarray:
        """This window cut out of a (ny, nx) array on ``parent`` (wrapping in longitude if needed)."""
        if parent_values.shape != parent.shape:
            raise ValueError(f"array shape {parent_values.shape} != {parent.name} grid {parent.shape}")
        r, c = self.offset_in(parent)
        cols = (c + np.arange(self.nx)) % parent.nx
        return parent_values[r:r + self.ny][:, cols]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> GridSpec:
        return GridSpec(name=str(d["name"]), lon0=float(d["lon0"]), lat0=float(d["lat0"]), dlon=float(d["dlon"]),
                        dlat=float(d["dlat"]), nx=int(d["nx"]), ny=int(d["ny"]))


# Native global grids of each source family; the NZ windows in config must line up with these.
GSMAP_GLOBAL = GridSpec("gsmap_global", lon0=0.05, lat0=59.95, dlon=0.1, dlat=-0.1, nx=3600, ny=1200)
ECMWF_GLOBAL = GridSpec("ecmwf_global", lon0=-180.0, lat0=90.0, dlon=0.1, dlat=-0.1, nx=3600, ny=1801)
NATIVE = {"gsmap": GSMAP_GLOBAL, "ecmwf": ECMWF_GLOBAL}


# --- hours --------------------------------------------------------------------------------------
def hour_index(t: datetime) -> int:
    """Whole UTC hour ``t`` as hours since the epoch (``t`` must be timezone-aware and on the hour)."""
    if t.tzinfo is None:
        raise ValueError(f"{t!r}: rainfall times must be timezone-aware (UTC)")
    s = (t - _EPOCH).total_seconds()
    if s % 3600:
        raise ValueError(f"{t.isoformat()}: not a whole hour")
    return int(s // 3600)


def hour_time(h: int) -> datetime:
    return _EPOCH + timedelta(hours=int(h))


def floor_hour(t: datetime) -> datetime:
    """The latest whole hour at or before ``t`` (UTC)."""
    t = t.astimezone(UTC)
    return t.replace(minute=0, second=0, microsecond=0)


def metres_per_degree(lat) -> tuple[np.ndarray, float]:
    """(metres per degree of longitude at ``lat``, metres per degree of latitude), for error reports."""
    return 111_320.0 * np.cos(np.radians(lat)), 110_950.0

"""Region definitions (regions/*.toml)."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pyproj import Transformer

NZTM = "EPSG:2193"
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class KnownRun:
    river: str
    status: str  # "listed" (in guides) or "unlisted"
    outlet_wgs84: tuple[float, float]
    rec2_cum_area_km2: float | None = None


@dataclass
class Region:
    name: str
    description: str
    role: str
    bbox_wgs84: tuple[float, float, float, float]
    dem_coarse: str
    lidar: str | None = None
    routing_res: float = 8.0
    known_runs: list[KnownRun] = field(default_factory=list)
    nztm_box: tuple[float, float, float, float] | None = None  # explicit NZTM box (national tiles)
    root: Path | None = None  # data directory override

    def bbox_nztm(self, res: float = 8.0) -> tuple[float, float, float, float]:
        """Bbox in NZTM, expanded outward to whole multiples of ``res``."""
        if self.nztm_box is not None:
            return self.nztm_box
        t = Transformer.from_crs("EPSG:4326", NZTM, always_xy=True)
        x0, y0, x1, y1 = t.transform_bounds(*self.bbox_wgs84, densify_pts=21)
        return (
            math.floor(x0 / res) * res,
            math.floor(y0 / res) * res,
            math.ceil(x1 / res) * res,
            math.ceil(y1 / res) * res,
        )

    def data_dir(self, root: Path | None = None) -> Path:
        d = self.root if self.root is not None and root is None else (root or REPO_ROOT / "data") / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d


def regions_dir() -> Path:
    for cand in (Path.cwd() / "regions", REPO_ROOT / "regions"):
        if cand.is_dir():
            return cand
    raise FileNotFoundError("no regions/ directory found")


def load_region(name_or_path: str) -> Region:
    p = Path(name_or_path)
    if not p.suffix:
        p = regions_dir() / f"{name_or_path}.toml"
    d = tomllib.loads(p.read_text())
    runs = d.get("known_runs", [])
    if isinstance(runs, dict):  # legacy form: {rivers = [...]}
        runs = [{"river": r, "status": "listed", "outlet_wgs84": [math.nan, math.nan]} for r in runs["rivers"]]
    return Region(
        name=d["name"],
        description=d.get("description", ""),
        role=d.get("role", "search"),
        bbox_wgs84=tuple(d["bbox_wgs84"]),
        dem_coarse=d["dem"]["coarse"],
        lidar=d["dem"].get("lidar"),
        routing_res=float(d["dem"].get("routing_res", 8.0)),
        known_runs=[
            KnownRun(r["river"], r.get("status", "listed"), tuple(r["outlet_wgs84"]), r.get("rec2_cum_area_km2"))
            for r in runs
        ],
    )


def list_regions() -> list[str]:
    return sorted(p.stem for p in regions_dir().glob("*.toml"))

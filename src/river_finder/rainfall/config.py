"""``config/rainfall.toml``: windows, which source feeds which metric, data paths, grids and ECMWF settings.

Validated on load; any error names the key. Paths are relative to the repository root
(``paths.nz_data``) or to ``nz_data`` (``tiles``, ``segments``, ``rain``); the CLI's ``--data``
replaces ``nz_data``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from ..regions import REPO_ROOT
from .buffer import SLOTS
from .ecmwf import ECMWF_URLS, OPENMETEO_URL, READERS
from .grid import NATIVE, GridSpec
from .sources import family_products

CONFIG_PATH = REPO_ROOT / "config" / "rainfall.toml"
_TOP = {"windows", "paths", "observed", "forecast", "grids", "ecmwf"}
_PATHS = {"nz_data", "tiles", "segments", "rain"}
_GRID = {"lon0", "lat0", "dlon", "dlat", "nx", "ny"}
_ECMWF = {"reader", "obs_leads", "obs_runs", "search_hours", "cache", "keep_days", "ecmwf_urls", "openmeteo_url"}


@dataclass(frozen=True)
class EcmwfConfig:
    """``[ecmwf]``: where hourly IFS rain comes from and which leads make the observed proxy (see :mod:`.ecmwf`)."""

    reader: str = "openmeteo-s3"
    obs_leads: tuple[int, int] = (1, 18)
    obs_runs: int = 6
    search_hours: int = 48
    cache: str = "cache"
    keep_days: int = 3
    ecmwf_urls: tuple[str, ...] = ECMWF_URLS
    openmeteo_url: str = OPENMETEO_URL


@dataclass(frozen=True)
class RainConfig:
    windows: tuple[int, ...]
    observed_source: str
    compare: tuple[str, ...]
    forecast_source: str
    grids: dict[str, GridSpec]
    nz_data: Path
    tiles: str = "tiles"
    segments: str = "segments_table.pkl"
    rain: str = "rain"
    ecmwf: EcmwfConfig = EcmwfConfig()

    @property
    def tiles_dir(self) -> Path:
        return self.nz_data / self.tiles

    @property
    def segments_path(self) -> Path:
        return self.nz_data / self.segments

    @property
    def rain_dir(self) -> Path:
        return self.nz_data / self.rain

    @property
    def observed_families(self) -> tuple[str, ...]:
        return (self.observed_source, *self.compare)

    def weights_path(self, family: str) -> Path:
        return self.rain_dir / f"weights_{family}.npz"

    def buffer_path(self, kind: str, family: str) -> Path:
        return self.rain_dir / f"{'obs' if kind == 'observed' else 'fc'}_{family}.npz"

    @property
    def totals_path(self) -> Path:
        """Upstream totals of the observed and forecast sources (rf-t26.6), ``totals.npz`` + ``.json``."""
        return self.rain_dir / "totals.npz"

    @property
    def network_path(self) -> Path:
        """The reach network the totals run on (``upstream.Network``), cached from the segment table."""
        return self.rain_dir / "network.npz"

    @property
    def cache_dir(self) -> Path | None:
        """Decoded-field cache of the ECMWF readers (None when ``ecmwf.cache`` is empty)."""
        return self.rain_dir / self.ecmwf.cache if self.ecmwf.cache else None

    def with_data(self, nz_data: Path | None) -> RainConfig:
        return self if nz_data is None else replace(self, nz_data=Path(nz_data))


def _err(msg: str):
    raise ValueError(f"rainfall config: {msg}")


def _keys(d, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        _err(f"[{where}] must be a table")
    extra = set(d) - allowed
    if extra:
        _err(f"unknown key(s) in [{where}]: {', '.join(sorted(extra))}")
    return d


def _windows(v, where: str) -> tuple[int, ...]:
    if not isinstance(v, list) or not v:
        _err(f"{where} must be a non-empty list of hours")
    for x in v:
        if isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= SLOTS:
            _err(f"{where}: {x!r} is not a whole number of hours in 1..{SLOTS}")
    if len(set(v)) != len(v):
        _err(f"{where}: duplicate windows")
    return tuple(sorted(v))


def _family(name, grids: dict, kind: str, where: str) -> str:
    if not isinstance(name, str) or name not in grids:
        _err(f"{where} = {name!r}: no [grids.{name}] defined (have {', '.join(grids)})")
    if not family_products(name, kind):
        _err(f"{where} = {name!r}: that source has no {kind} product")
    return name


def _int(v, where: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        _err(f"{where} = {v!r}: expected a whole number in {lo}..{hi}")
    return v


def _ecmwf(d) -> EcmwfConfig:
    e = _keys(d, _ECMWF, "ecmwf")
    base = EcmwfConfig()
    reader = e.get("reader", base.reader)
    if reader not in READERS:
        _err(f"ecmwf.reader = {reader!r}: expected one of {', '.join(READERS)}")
    leads = e.get("obs_leads", list(base.obs_leads))
    if not isinstance(leads, list) or len(leads) != 2:
        _err("ecmwf.obs_leads must be [first, last] lead hours")
    lo, hi = (_int(v, "ecmwf.obs_leads", 1, 144) for v in leads)
    if hi - lo + 1 < 6:
        _err(f"ecmwf.obs_leads {leads}: must span at least 6 hours, the time between runs, or hours go missing")
    urls = e.get("ecmwf_urls", list(base.ecmwf_urls))
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.startswith("http") for u in urls):
        _err("ecmwf.ecmwf_urls must be a non-empty list of http(s) URLs")
    om = e.get("openmeteo_url", base.openmeteo_url)
    if not isinstance(om, str) or not om.startswith("http"):
        _err("ecmwf.openmeteo_url must be an http(s) URL")
    cache = e.get("cache", base.cache)
    if not isinstance(cache, str):
        _err("ecmwf.cache must be a string (empty: no cache)")
    return EcmwfConfig(reader, (lo, hi), _int(e.get("obs_runs", base.obs_runs), "ecmwf.obs_runs", 1, 12),
                       _int(e.get("search_hours", base.search_hours), "ecmwf.search_hours", 6, 168), cache,
                       _int(e.get("keep_days", base.keep_days), "ecmwf.keep_days", 0, 30), tuple(urls), om)


def parse_config(d: dict, root: Path = REPO_ROOT) -> RainConfig:
    _keys(d, _TOP, "top level")
    windows = _windows(d.get("windows", [1, 3, 6, 12, 24]), "windows")
    grids = {}
    for name, g in _keys(d.get("grids", {}), set(NATIVE), "grids").items():
        _keys(g, _GRID, f"grids.{name}")
        missing = _GRID - set(g)
        if missing:
            _err(f"[grids.{name}] lacks {', '.join(sorted(missing))}")
        for k in ("nx", "ny"):
            if isinstance(g[k], bool) or not isinstance(g[k], int):
                _err(f"grids.{name}.{k} must be an integer")
        try:
            spec = GridSpec(name=name, **{k: g[k] for k in _GRID})
            spec.offset_in(NATIVE[name])
        except (TypeError, ValueError) as e:
            _err(f"[grids.{name}]: {e}")
        grids[name] = spec
    if not grids:
        _err("no [grids.*] defined")
    obs = _keys(d.get("observed", {}), {"source", "compare"}, "observed")
    fc = _keys(d.get("forecast", {}), {"source"}, "forecast")
    source = _family(obs.get("source"), grids, "observed", "observed.source")
    compare = obs.get("compare", [])
    if not isinstance(compare, list):
        _err("observed.compare must be a list")
    compare = tuple(_family(c, grids, "observed", "observed.compare") for c in compare)
    if source in compare or len(set(compare)) != len(compare):
        _err("observed.compare must not repeat observed.source or itself")
    forecast = _family(fc.get("source"), grids, "forecast", "forecast.source")
    paths = _keys(d.get("paths", {}), _PATHS, "paths")
    for k, v in paths.items():
        if not isinstance(v, str) or not v:
            _err(f"paths.{k} must be a non-empty string")
    nz = Path(paths.get("nz_data", "data/nz"))
    return RainConfig(windows, source, compare, forecast, grids, nz if nz.is_absolute() else root / nz,
                      paths.get("tiles", "tiles"), paths.get("segments", "segments_table.pkl"),
                      paths.get("rain", "rain"), _ecmwf(d.get("ecmwf", {})))


def load_config(path: Path | None = None, nz_data: Path | None = None) -> RainConfig:
    path = Path(path) if path else CONFIG_PATH
    with open(path, "rb") as fh:
        d = tomllib.load(fh)
    return parse_config(d).with_data(nz_data)

"""``config/rainfall.toml``: windows, which source feeds which metric, data paths, grids, ECMWF, gauge and
gauge-scaling settings.

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
_TOP = {"windows", "paths", "observed", "forecast", "grids", "ecmwf", "gauges", "scaling"}
_PATHS = {"nz_data", "tiles", "segments", "rain"}
_GRID = {"lon0", "lat0", "dlon", "dlat", "nx", "ny"}
_ECMWF = {"reader", "obs_leads", "obs_runs", "search_hours", "cache", "keep_days", "ecmwf_urls", "openmeteo_url"}
_GAUGES = {"dir", "days", "keep_days", "refetch_hours", "chunk_days", "request_gap_s", "timeout_s", "retries",
           "max_mm_per_hour", "inactive_days", "recheck_days", "councils"}
_COUNCIL = {"name", "kind", "enabled", "base_url", "hts", "measurements", "timezone", "method", "quality", "region",
            "licence", "attribution", "source"}
_SCALING = {"apply", "publish", "dir", "days", "keep_days", "min_gauges", "min_usable", "min_gauge_mm", "min_grid_mm",
            "clamp", "max_step", "min_change", "min_update_hours", "dedupe_km", "dedupe_min_wet_hours",
            "dedupe_equal_share", "dedupe_equal_mm", "colocated_m"}
_SCALING_SOURCE = {"products", "readers"}
# hilltop: a Hilltop Server (:mod:`.hilltop`); ecan-portal: Environment Canterbury's Open Data Portal
# (:mod:`.ecanportal`: hourly totals per site, no .hts file).
KINDS = ("hilltop", "ecan-portal")
# Time zone of the times a server sends and expects: NZST = UTC+12 all year (the NEMS rule for recorders);
# Pacific/Auckland = NZ wall clock (NZDT, UTC+13, in summer); UTC. Times that carry an offset are read as given.
TIMEZONES = ("NZST", "Pacific/Auckland", "UTC")
# raw: every record, summed here into hours ending (handles <Gap/> and partial hours);
# total-end / total-start: the server's own Method=Total&Interval=1 hour, stamped at the end / start of the hour.
METHODS = ("raw", "total-end", "total-start")


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
class CouncilConfig:
    """``[gauges.councils.<id>]``: one council's gauge server (see :mod:`.hilltop`, :mod:`.ecanportal`,
    ``docs/rain-gauges.md``)."""

    id: str
    name: str
    base_urls: tuple[str, ...]               # server roots, tried in order (e.g. https, then http)
    hts: tuple[str, ...]                     # hilltop: candidate .hts files, tried in order: the first with rain wins
    # hilltop: the rain series to look for (case-insensitive); per site the one whose record reaches nearest to now
    # is used, ties (within 6 h) going to the earlier name
    measurements: tuple[str, ...] = ("Rainfall",)
    enabled: bool = True
    timezone: str = "NZST"
    method: str = "raw"
    quality: bool = True                     # ask for quality codes (ShowQuality and the StdQualSeries)
    region: str = ""                         # scaling region when a gauge cannot be placed (network region name)
    licence: str = ""
    attribution: str = ""
    source: str = ""                         # where the endpoint is documented
    kind: str = "hilltop"                    # hilltop | ecan-portal


@dataclass(frozen=True)
class GaugesConfig:
    """``[gauges]``: rain-gauge ingestion (rf-t26.4, :mod:`.gauges`)."""

    dir: str = "gauges"          # under <rain>
    days: int = 35               # hours fetched: the last ``days`` x 24 whole hours
    keep_days: int = 60          # cached hours older than this are dropped
    refetch_hours: int = 48      # the newest hours are fetched again on every run (late telemetry, edits)
    chunk_days: int = 10         # longest span of one GetData request
    request_gap_s: float = 1.0   # pause between two requests to one server
    timeout_s: float = 60.0
    retries: int = 4             # per request, on HTTP 429/5xx and network errors (backoff doubling from ~1 s)
    max_mm_per_hour: float = 200.0  # hourly totals above this are suspect
    inactive_days: int = 30      # a gauge whose record ended longer ago than this is inactive: no requests ...
    recheck_days: int = 7        # ... until this long after its last check (Hilltop: the site's MeasurementList)
    councils: tuple[CouncilConfig, ...] = ()

    def council(self, cid: str) -> CouncilConfig:
        for c in self.councils:
            if c.id == cid:
                return c
        raise KeyError(f"no [gauges.councils.{cid}] (have {', '.join(c.id for c in self.councils)})")


@dataclass(frozen=True)
class ScalingSource:
    """``[scaling.<family>]``: which products of a source family the family's factors scale, and (ECMWF) the
    readers the grid side of the comparison is backfilled from, in order (:mod:`.scaling`)."""

    family: str
    products: tuple[str, ...]
    readers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScalingConfig:
    """``[scaling]``: gauge scale factors per source family and region (rf-t26.5, :mod:`.scaling`,
    ``docs/rainfall-scaling.md``)."""

    apply: bool = True             # rain ecmwf (and rain scale) scale the buffers with the current factors
    publish: bool = False          # rain publish: gauge-scaled values only when true (rf-t26.14: licences)
    dir: str = "scaling"           # under <rain>: <family>_cells.npz, factors_<family>.json (+ .history.jsonl)
    days: int = 30                 # the comparison window: the last days x 24 hours
    keep_days: int = 40            # hours of the grid-cell store older than this are dropped
    min_gauges: int = 3            # fewer usable (deduplicated) gauges in a region: factor 1, version 0
    min_usable: float = 0.8        # share of the window's hours a gauge needs (gauge usable and a grid value)
    min_gauge_mm: float = 30.0     # mean gauge total per gauge over the window below which no ratio is formed
    min_grid_mm: float = 30.0      # ... and the same for the grid at the gauges' cells (the factor is held)
    clamp: tuple[float, float] = (0.5, 2.0)   # factor limits
    max_step: float = 1.25         # one update moves a factor by at most this ratio either way
    min_change: float = 0.02       # a change smaller than this keeps the current factor and version
    min_update_hours: int = 20     # a region gets a new version at most this often (--force overrides)
    dedupe_km: float = 3.0         # gauges this close are compared as possible copies of one station
    dedupe_min_wet_hours: int = 10  # common hours with rain at either needed to compare two series
    dedupe_equal_share: float = 0.95  # copies: this share of those hours agree within dedupe_equal_mm
    dedupe_equal_mm: float = 0.05
    colocated_m: float = 100.0     # distinct gauges this close are listed as co-located (with their lag)
    sources: tuple[ScalingSource, ...] = (ScalingSource("ecmwf", ("ECMWF_OBS", "ECMWF_FC"),
                                                        ("openmeteo-s3", "ecmwf-s3")),)

    def source(self, family: str) -> ScalingSource | None:
        for s in self.sources:
            if s.family == family:
                return s
        return None


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
    gauges: GaugesConfig = GaugesConfig()
    scaling: ScalingConfig = ScalingConfig()

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

    @property
    def gauges_dir(self) -> Path:
        """Rain-gauge cache and outputs (rf-t26.4): ``sites.json``, ``series/``, ``gauges_hourly.npz`` (+ ``.json``)."""
        return self.rain_dir / self.gauges.dir

    @property
    def scaling_dir(self) -> Path:
        """Gauge scaling (rf-t26.5): ``<family>_cells.npz``, ``factors_<family>.json`` (+ ``.history.jsonl``)."""
        return self.rain_dir / self.scaling.dir

    def cells_path(self, family: str) -> Path:
        """The grid side of the gauge comparison: hourly mm of ``family`` at the gauges' cells (:mod:`.scaling`)."""
        return self.scaling_dir / f"{family}_cells.npz"

    def factors_path(self, family: str) -> Path:
        """Current scale factors of ``family`` per region; the version history is ``<stem>.history.jsonl``."""
        return self.scaling_dir / f"factors_{family}.json"

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


def _num(v, where: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
        _err(f"{where} = {v!r}: expected a number in {lo}..{hi}")
    return float(v)


def _strs(v, where: str, suffix: str = "") -> tuple[str, ...]:
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
        _err(f"{where} must be a non-empty list of strings")
    if suffix and not all(x.lower().endswith(suffix) for x in v):
        _err(f"{where}: every entry must end with {suffix}")
    return tuple(x.strip() for x in v)


def _council(cid: str, d) -> CouncilConfig:
    where = f"gauges.councils.{cid}"
    c = _keys(d, _COUNCIL, where)
    kind = c.get("kind", "hilltop")
    if kind not in KINDS:
        _err(f"{where}.kind = {kind!r}: expected one of {', '.join(KINDS)}")
    for k in ("name", "base_url") + (("hts",) if kind == "hilltop" else ()):
        if k not in c:
            _err(f"[{where}] lacks {k}")
    urls = c["base_url"] if isinstance(c["base_url"], list) else [c["base_url"]]
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or "?" in url or ".hts" in url:
            _err(f"{where}.base_url = {url!r}: expected http(s) server roots without a query (the .hts file goes "
                 "in hts)")
    if not urls:
        _err(f"{where}.base_url must not be empty")
    tz = c.get("timezone", "NZST")
    if tz not in TIMEZONES:
        _err(f"{where}.timezone = {tz!r}: expected one of {', '.join(TIMEZONES)}")
    method = c.get("method", "raw")
    if method not in METHODS:
        _err(f"{where}.method = {method!r}: expected one of {', '.join(METHODS)}")
    for k in ("enabled", "quality"):
        if not isinstance(c.get(k, True), bool):
            _err(f"{where}.{k} must be true or false")
    for k in ("name", "region", "licence", "attribution", "source"):
        if not isinstance(c.get(k, ""), str):
            _err(f"{where}.{k} must be a string")
    hts = _strs(c["hts"], f"{where}.hts", ".hts") if "hts" in c else ()
    return CouncilConfig(cid, c["name"], tuple(u if u.endswith("/") else u + "/" for u in urls), hts,
                         _strs(c.get("measurements", ["Rainfall"]), f"{where}.measurements"), c.get("enabled", True),
                         tz, method, c.get("quality", True), c.get("region", ""), c.get("licence", ""),
                         c.get("attribution", ""), c.get("source", ""), kind)


def _gauges(d) -> GaugesConfig:
    g = _keys(d, _GAUGES, "gauges")
    base = GaugesConfig()
    gdir = g.get("dir", base.dir)
    if not isinstance(gdir, str) or not gdir:
        _err("gauges.dir must be a non-empty string")
    councils = []
    table = g.get("councils", {})
    if not isinstance(table, dict):
        _err("[gauges.councils] must be a table of [gauges.councils.<id>] tables")
    for cid, c in table.items():
        if not isinstance(cid, str) or not cid.replace("_", "").isalnum() or cid.lower() != cid:
            _err(f"[gauges.councils.{cid}]: council ids are lower-case letters, digits and _")
        councils.append(_council(cid, c))
    keep = _int(g.get("keep_days", base.keep_days), "gauges.keep_days", 1, 366)
    days = _int(g.get("days", base.days), "gauges.days", 1, 366)
    if days > keep:
        _err(f"gauges.days ({days}) must not exceed gauges.keep_days ({keep})")
    return GaugesConfig(gdir, days, keep,
                        _int(g.get("refetch_hours", base.refetch_hours), "gauges.refetch_hours", 0, 24 * 30),
                        _int(g.get("chunk_days", base.chunk_days), "gauges.chunk_days", 1, 366),
                        _num(g.get("request_gap_s", base.request_gap_s), "gauges.request_gap_s", 0, 60),
                        _num(g.get("timeout_s", base.timeout_s), "gauges.timeout_s", 1, 600),
                        _int(g.get("retries", base.retries), "gauges.retries", 0, 10),
                        _num(g.get("max_mm_per_hour", base.max_mm_per_hour), "gauges.max_mm_per_hour", 1, 10_000),
                        _int(g.get("inactive_days", base.inactive_days), "gauges.inactive_days", 1, 3660),
                        _int(g.get("recheck_days", base.recheck_days), "gauges.recheck_days", 0, 366),
                        tuple(councils))


def _bool(v, where: str) -> bool:
    if not isinstance(v, bool):
        _err(f"{where} must be true or false")
    return v


def _scaling(d, grids: dict) -> ScalingConfig:
    s = _keys(d, _SCALING | set(grids), "scaling")
    base = ScalingConfig()
    sdir = s.get("dir", base.dir)
    if not isinstance(sdir, str) or not sdir:
        _err("scaling.dir must be a non-empty string")
    clamp = s.get("clamp", list(base.clamp))
    if not isinstance(clamp, list) or len(clamp) != 2:
        _err("scaling.clamp must be [lowest, highest]")
    lo, hi = (_num(v, "scaling.clamp", 0.01, 100.0) for v in clamp)
    if not lo <= 1.0 <= hi:
        _err(f"scaling.clamp {clamp}: must contain 1")
    days = _int(s.get("days", base.days), "scaling.days", 1, 120)
    keep = _int(s.get("keep_days", base.keep_days), "scaling.keep_days", 1, 366)
    if keep < days:
        _err(f"scaling.keep_days ({keep}) must be at least scaling.days ({days})")
    sources = {src.family: src for src in base.sources}
    for fam in grids:
        if fam not in s:
            continue
        t = _keys(s[fam], _SCALING_SOURCE, f"scaling.{fam}")
        prods = _strs(t.get("products", list(sources[fam].products) if fam in sources else []),
                      f"scaling.{fam}.products")
        for tag in prods:
            if tag not in family_products(fam):
                _err(f"scaling.{fam}.products: {tag!r} is not a {fam} product (have {', '.join(family_products(fam))})")
        readers = t.get("readers", list(sources[fam].readers) if fam in sources else [])
        if fam == "ecmwf":
            readers = _strs(readers, "scaling.ecmwf.readers")
            bad = [r for r in readers if r not in READERS]
            if bad or len(set(readers)) != len(readers):
                _err(f"scaling.ecmwf.readers {list(readers)}: expected distinct names from {', '.join(READERS)}")
        elif readers:
            _err(f"scaling.{fam}.readers: only ECMWF has readers")
        sources[fam] = ScalingSource(fam, prods, tuple(readers))
    step = _num(s.get("max_step", base.max_step), "scaling.max_step", 1.0, 10.0)
    return ScalingConfig(
        _bool(s.get("apply", base.apply), "scaling.apply"), _bool(s.get("publish", base.publish), "scaling.publish"),
        sdir, days, keep, _int(s.get("min_gauges", base.min_gauges), "scaling.min_gauges", 1, 1000),
        _num(s.get("min_usable", base.min_usable), "scaling.min_usable", 0.0, 1.0),
        _num(s.get("min_gauge_mm", base.min_gauge_mm), "scaling.min_gauge_mm", 0.0, 10_000.0),
        _num(s.get("min_grid_mm", base.min_grid_mm), "scaling.min_grid_mm", 0.0, 10_000.0), (lo, hi), step,
        _num(s.get("min_change", base.min_change), "scaling.min_change", 0.0, 1.0),
        _int(s.get("min_update_hours", base.min_update_hours), "scaling.min_update_hours", 0, 24 * 60),
        _num(s.get("dedupe_km", base.dedupe_km), "scaling.dedupe_km", 0.0, 50.0),
        _int(s.get("dedupe_min_wet_hours", base.dedupe_min_wet_hours), "scaling.dedupe_min_wet_hours", 1, 10_000),
        _num(s.get("dedupe_equal_share", base.dedupe_equal_share), "scaling.dedupe_equal_share", 0.5, 1.0),
        _num(s.get("dedupe_equal_mm", base.dedupe_equal_mm), "scaling.dedupe_equal_mm", 0.0, 10.0),
        _num(s.get("colocated_m", base.colocated_m), "scaling.colocated_m", 0.0, 10_000.0),
        tuple(v for f, v in sources.items() if f in grids))


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
                      paths.get("rain", "rain"), _ecmwf(d.get("ecmwf", {})), _gauges(d.get("gauges", {})),
                      _scaling(d.get("scaling", {}), grids))


def load_config(path: Path | None = None, nz_data: Path | None = None) -> RainConfig:
    path = Path(path) if path else CONFIG_PATH
    with open(path, "rb") as fh:
        d = tomllib.load(fh)
    return parse_config(d).with_data(nz_data)

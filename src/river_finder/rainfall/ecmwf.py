"""ECMWF IFS (rf-t26.3): hourly total precipitation for NZ, as an observed proxy and as a forecast.

Readers
-------
Two readers give the same thing, hourly mm on the ECMWF 0.1° NZ window (``[grids.ecmwf]``), and
``[ecmwf] reader`` in ``config/rainfall.toml`` picks one (``rain ecmwf --reader`` overrides it):

- ``openmeteo-s3`` (:class:`OpenMeteoReader`, the default): Open-Meteo's copy of the IFS HRES run
  on its native O1280 octahedral reduced Gaussian grid (~9 km), one ``.om`` file per valid hour
  (``s3://openmeteo/data_spatial/ecmwf_ifs/<Y>/<m>/<d>/<HH>00Z/<valid>.om``, CC-BY 4.0, about 7 days
  kept). ``precipitation`` is the rain of the preceding hour in mm, stored in 0.1 mm steps. Only
  the O1280 rows that bracket the window are read (the ~120 MB file costs two range requests,
  ~0.74 MB: its tail with the metadata, then the compressed chunks, through
  :class:`.fetch.RangeFile`); they are interpolated bilinearly to the 0.1° cell centres (linear in
  longitude along the two bracketing Gaussian rows, then linear in latitude; a sparse matrix built
  once). The file's ``crs_wkt``, ``forecast_reference_time`` and ``valid_time`` are checked.
- ``ecmwf-s3`` (:class:`GribReader`): ECMWF open data ``ifs/0p1/oper`` from the bucket mirrors in
  ``[ecmwf] ecmwf_urls`` (AWS S3, then Google Cloud; data.ecmwf.int itself is not reachable from
  here). For each step: the ``.index`` (JSON lines, ~10 kB) gives the byte range of ``tp`` in the
  ``-oper-fc-sfc.grib2`` file (~80 MB), and one range request fetches that message only (~1.1 MB,
  global, CCSDS-packed). Decoded with ``gribberish``; checked bit for bit against ecCodes 2.50 on
  the 2026-11-19 06z ``tp`` message. Sections 1, 3 and 4 are parsed here and must say: tp
  (discipline 0, category 1, number 193 in m, or number 8 in kg m⁻²), accumulated from the run
  start to the step, on the regular 0.1° grid from 90N 180E (3600 x 1801, scan mode 0), so the
  window is cut out exactly with :meth:`.grid.GridSpec.crop` from :data:`.grid.ECMWF_GLOBAL`.

Time steps (checked on the runs of 2026-11-18 and -19)
------------------------------------------------------
- ECMWF 0p1: every run has steps 0, 3, 6, ... 144 h; the 00z and 12z runs go on 150, 156, ... 360 h.
  There are no hourly steps, at short leads or anywhere, for the 06z/18z runs either. A run is
  complete about 8.5 h (06z, 18z) to 9.5 h (00z, 12z) after its base time.
- Open-Meteo ``ecmwf_ifs``: hourly files from lead 0 to 90 h, then 3-hourly to 144 h; files appear
  while the run is converted (leads 1-6 of the 12z run were there by 17:34Z) and a run is complete
  about 6 h after its base time.

Hourly values
-------------
``openmeteo-s3`` is hourly as published: hour ``run+L`` is the file of lead ``L``. Only leads
whose previous lead also has a file are used (so 1..90 h; the 3-hourly tail is not).

``ecmwf-s3`` splits each 3-hour accumulation evenly: for lead ``L`` in the step interval
``(s0, s1]``, ``mm = (tp(s1) - tp(s0)) / (s1 - s0)``, with ``tp(0) = 0``. Packing noise can make a
difference a little negative (``tp`` is stored to 2^-14 m ≈ 0.06 mm); it is clipped at 0.

Comparison on the 2026-11-19 06z run, leads 1-24, 18 753 cells of the NZ window:

- 3-hour totals, Open-Meteo (sum of 3 hours, regridded) vs the 0p1 GRIB: ratio 0.98 and
  correlation 0.94 for cells with 0.3-1 mm, 0.98 and 0.97 for 1-5 mm; over the whole window
  Open-Meteo is 16 % lower, all of it in drizzle below 0.3 mm/3 h (0.1 mm/h storage steps).
- Splitting Open-Meteo's own 3-hour sums evenly, against its hourly values: hourly error
  (relative RMSE, wet hours) 54 %, the wettest hour 25 % too low; windows of 1, 2, 4, 7, 12 and
  23 h are off by 32, 25, 13, 9, 6 and 3.5 % (windows that start and end on step boundaries are
  exact). A mass-conserving linear split using the neighbouring 3-hour blocks only reduced these
  to 51 % and 29 % (1 h) at the cost of an extra step per run, so the GRIB path splits evenly.

- Both readers on the same run (06z), through the national weights: catchment-mean 24 h observed
  proxy 1.12 (Open-Meteo) vs 1.17 mm (GRIB), maximum 17.1 vs 17.5 mm; next 24 h 1.18 vs 1.25 mm,
  maximum 16.4 vs 16.0 mm. Downloads: 35.6 MB in 100 requests vs 25.6 MB in 38 (with 20 retries
  of S3 ``503 SlowDown``).

So Open-Meteo is the default: real hourly values, available 2-3 h sooner. The GRIB path stays
selectable: it is ECMWF's own product on exactly this grid and keeps drizzle.

Observed proxy (``ECMWF_OBS``)
------------------------------
Each hour comes from the newest run that has published it with a lead in ``[ecmwf] obs_leads``
(default 1-18 h). With a run every 6 h, the hours up to the newest run come from leads 1-6 of the
runs before it (they tile the timeline and are the shortest leads there are; ERA5 similarly builds
its accumulations from forecast steps 1-12 h; spin-up in the first hours of IFS precipitation is
small), and the hours after it from its own leads 1, 2, ... up to "now". The window ends at "now"
(the latest whole hour), so the forecast's first hour (now+1) follows it with no gap.

"Latest" is the newest run whose lead ``obs_leads[0]`` is out (and has ended), even while the rest of
it is still being converted: the plan takes the leads it lists so far and fills the hours after them
from the run before. The upper lead must reach "now" from the newest run until the next one appears:
the next run's first leads appear ~5-6 h after its base time with Open-Meteo, so "now" is up to
~11 h after the newest run, and up to ~15 h with the GRIB reader (runs complete 8.5-9.5 h after
their base time), hence 18. Longer leads only fill hours that no newer run has published: a missing
run's hours come from leads 7-12 of the run before it, and the buffer keeps the newest run for each
hour, so an hour written from a long lead is replaced by a short lead of the next run. If no run
reaches "now" within ``obs_leads`` (ECMWF late), the window ends at the newest hour one does and lags
"now" (``hours_behind_now``); hours inside the window that no run covers are reported missing.

``obs_runs`` runs are considered (the latest and the ones before it). The window starts 23 h before
"now", which is ``k`` = 5-11 h (Open-Meteo) after the latest run: for ``k`` >= 6, 4 runs cover the
24 h; for ``k`` <= 5 the 5th is needed; a 6th (the default) keeps them complete when one run is
missing. A run is only listed when no newer one has the hour, so the extra runs cost nothing
otherwise. These fields go into the observed ECMWF buffer beside the satellite (IMERG) buffer,
through the same interface, for side-by-side comparison; ``ECMWF_OBS`` has the lowest priority of
the observed products.

Forecast (``ECMWF_FC``)
-----------------------
The forecast buffer is anchored at "now" (the current whole UTC hour) and holds hours
``now+1 .. now+24`` from one run, the newest one that covers them all; each slot keeps its run and
lead, and the CLI report and ``fc_ecmwf.json`` give the run and its age (``now - run``).

Idempotence and caching
-----------------------
A buffer slot is keyed by its valid hour and is replaced, never added to, so running the same
ingest twice gives the same buffers (status ``updated``). Decoded NZ crops are kept under
``<rain>/cache/<reader>/<grid>/<run>/`` (75 kB per field; older than ``keep_days`` are pruned), so a
re-run downloads only the listings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import scipy.sparse as sp

from .buffer import SLOTS
from .fetch import Bucket, Http, NotFound, RangeFile
from .grid import ECMWF_GLOBAL, GridSpec, hour_index, hour_time
from .sources import HourlyField, RainfallSource

ECMWF_URLS = ("https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/",
              "https://storage.googleapis.com/ecmwf-open-data/")
OPENMETEO_URL = "https://openmeteo.s3.amazonaws.com/"
OPENMETEO_PREFIX = "data_spatial/ecmwf_ifs/"
RUN_EVERY = 6  # hours between runs (00, 06, 12, 18 UTC)
READERS = ("openmeteo-s3", "ecmwf-s3")

# (discipline, category, number) of total precipitation -> mm per unit of the packed value
TP_PARAMS = {(0, 1, 193): 1000.0, (0, 1, 8): 1.0}


def run_label(run: datetime) -> str:
    return f"{run:%Y-%m-%d %H}Z"


def parse_hour(s: str) -> datetime:
    """``YYYYMMDDHH`` (UTC) -> datetime."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})", s.strip())
    if not m:
        raise ValueError(f"{s!r}: expected YYYYMMDDHH")
    return datetime(*map(int, m.groups()), tzinfo=UTC)


def parse_run(s: str) -> datetime:
    """``YYYYMMDDHH`` (UTC) -> datetime; the hour must be a run hour (00/06/12/18)."""
    t = parse_hour(s)
    if t.hour % RUN_EVERY:
        raise ValueError(f"run {s!r}: IFS runs start at 00, 06, 12 and 18 UTC")
    return t


def floor_run(t: datetime) -> datetime:
    t = t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return t - timedelta(hours=t.hour % RUN_EVERY)


# --- GRIB2: just what a tp message needs ---------------------------------------------------------
@dataclass(frozen=True)
class GribTp:
    discipline: int
    category: int
    number: int
    run: datetime
    step0: int          # start of the accumulation (hours after the run)
    step1: int          # end of the accumulation
    stat: int           # type of statistical processing (1 = accumulation)
    grid: GridSpec
    scan: int
    packing: int        # data representation template (0 simple, 42 CCSDS, ...)

    @property
    def mm_per_unit(self) -> float:
        try:
            return TP_PARAMS[(self.discipline, self.category, self.number)]
        except KeyError:
            raise ValueError(f"GRIB parameter {self.discipline}/{self.category}/{self.number} is not total "
                             f"precipitation") from None


def _sm(b: bytes) -> int:
    """GRIB2 sign-magnitude integer (big-endian)."""
    v = int.from_bytes(b, "big")
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v


def parse_grib(buf: bytes) -> GribTp:
    """Header of the first GRIB2 message in ``buf`` (sections 0, 1, 3, 4, 5); raises on anything unexpected."""
    if buf[:4] != b"GRIB" or buf[7] != 2:
        raise ValueError("not a GRIB edition 2 message")
    total = struct.unpack(">Q", buf[8:16])[0]
    if total > len(buf) or buf[total - 4:total] != b"7777":
        raise ValueError(f"truncated GRIB message ({len(buf)} of {total} bytes)")
    discipline, sec, pos = buf[6], {}, 16
    while pos < total - 4:
        n, num = struct.unpack(">IB", buf[pos:pos + 5])
        sec.setdefault(num, buf[pos:pos + n])
        pos += n
    for k in (1, 3, 4, 5):
        if k not in sec:
            raise ValueError(f"GRIB message lacks section {k}")
    s1, s3, s4, s5 = sec[1], sec[3], sec[4], sec[5]
    year = struct.unpack(">H", s1[12:14])[0]
    run = datetime(year, s1[14], s1[15], s1[16], s1[17], s1[18], tzinfo=UTC)
    if struct.unpack(">H", s3[12:14])[0] != 0:
        raise ValueError("grid is not a regular lat/lon grid (template 3.0)")
    ni, nj = struct.unpack(">II", s3[30:38])
    basic = struct.unpack(">I", s3[38:42])[0]
    if basic not in (0, 0xFFFFFFFF):
        raise ValueError("grid uses a basic angle; not supported")
    la1, lo1, la2, lo2 = (_sm(s3[a:a + 4]) / 1e6 for a in (46, 50, 55, 59))
    di, dj = (struct.unpack(">I", s3[a:a + 4])[0] / 1e6 for a in (63, 67))
    scan = s3[71]
    if scan & 0x40:
        raise ValueError("grid rows run south to north; only north-to-south (scan mode 0) is supported")
    grid = GridSpec("grib", lon0=lo1, lat0=la1, dlon=di, dlat=-dj, nx=ni, ny=nj)
    tmpl = struct.unpack(">H", s4[7:9])[0]
    if tmpl != 8:
        raise ValueError(f"product template 4.{tmpl}: expected 4.8 (accumulation over a time interval)")
    category, number = s4[9], s4[10]
    if s4[17] != 1 or s4[48] != 1:
        raise ValueError("forecast time is not in hours")
    step0 = struct.unpack(">I", s4[18:22])[0]
    length = struct.unpack(">I", s4[49:53])[0]
    end = datetime(struct.unpack(">H", s4[34:36])[0], s4[36], s4[37], s4[38], s4[39], s4[40], tzinfo=UTC)
    step1 = step0 + length
    if end != run + timedelta(hours=step1):
        raise ValueError(f"GRIB time range {step0}-{step1} h does not end at {end:%Y-%m-%d %H:%M}")
    packing = struct.unpack(">H", s5[9:11])[0]
    return GribTp(discipline, category, number, run, step0, step1, s4[46], grid, scan, packing)


def _same_lon(a: float, b: float) -> bool:
    return abs((a - b + 180.0) % 360.0 - 180.0) < 1e-6


def decode_grib_tp(buf: bytes, window: GridSpec, native: GridSpec = ECMWF_GLOBAL) -> tuple[np.ndarray, GribTp]:
    """(mm accumulated from the run start, cut to ``window``; the header) of one GRIB2 tp message."""
    import gribberish

    info = parse_grib(buf)
    g = info.grid
    if not ((g.nx, g.ny) == (native.nx, native.ny) and abs(g.lat0 - native.lat0) < 1e-6
            and _same_lon(g.lon0, native.lon0) and abs(g.dlon - native.dlon) < 1e-6
            and abs(g.dlat - native.dlat) < 1e-6):
        raise ValueError(f"GRIB grid {g.nx}x{g.ny} from ({g.lon0}, {g.lat0}) step {g.dlon} is not {native.name}")
    if info.stat != 1:
        raise ValueError(f"statistical processing {info.stat}: expected 1 (accumulation)")
    vals = np.asarray(gribberish.parse_grib_message(buf, 0).data(), dtype=np.float64)
    if vals.size != g.nx * g.ny:
        raise ValueError(f"decoded {vals.size} values, grid has {g.nx * g.ny}")
    mm = window.crop(vals.reshape(g.ny, g.nx), native) * info.mm_per_unit
    return mm.astype(np.float32), info


# --- O1280 ---------------------------------------------------------------------------------------
def gaussian_latitudes(n: int) -> np.ndarray:
    """The 2n Gaussian latitudes (degrees, north to south): roots of the Legendre polynomial P_2n."""
    x, _ = np.polynomial.legendre.leggauss(2 * n)
    return np.degrees(np.arcsin(x))[::-1].copy()


class OctahedralGrid:
    """ECMWF octahedral reduced Gaussian grid O<n>: 2n rows; row i (1-based from either pole) has 4i + 16
    points from 0° eastwards; values are stored row by row from the north."""

    def __init__(self, n: int = 1280):
        self.n = int(n)
        north = 4 * np.arange(1, self.n + 1, dtype=np.int64) + 16
        self.nlon = np.concatenate([north, north[::-1]])
        self.offset = np.concatenate([[0], np.cumsum(self.nlon)])
        self.lat = gaussian_latitudes(self.n)

    @property
    def size(self) -> int:
        return int(self.offset[-1])

    def bilinear(self, grid: GridSpec) -> tuple[int, int, sp.csr_matrix]:
        """``(i0, i1, M)``: ``M @ values[i0:i1]`` gives the grid's cell-centre values (C order)."""
        lat = self.lat
        rows_n = np.searchsorted(-lat, -grid.lats, side="right") - 1   # last row at or north of each latitude
        if rows_n.min() < 0 or rows_n.max() >= len(lat) - 1:
            raise ValueError(f"grid {grid.name} reaches beyond the outer Gaussian rows")
        r0, r1 = int(rows_n.min()), int(rows_n.max()) + 1
        i0, i1 = int(self.offset[r0]), int(self.offset[r1 + 1])
        lon = np.mod(grid.lons, 360.0)
        ii, jj, ww = [], [], []
        for k, rn in enumerate(rows_n):
            wlat = (lat[rn] - grid.lats[k]) / (lat[rn] - lat[rn + 1])
            for r, wr in ((rn, 1.0 - wlat), (rn + 1, wlat)):
                n = int(self.nlon[r])
                pos = lon / (360.0 / n)
                a = np.floor(pos).astype(np.int64)
                f = pos - a
                cells = k * grid.nx + np.arange(grid.nx)
                for idx, wl in ((a % n, 1.0 - f), ((a + 1) % n, f)):
                    ii.append(cells)
                    jj.append(self.offset[r] + idx - i0)
                    ww.append(wr * wl)
        m = sp.csr_matrix((np.concatenate(ww), (np.concatenate(ii), np.concatenate(jj))),
                          shape=(grid.size, i1 - i0))
        m.eliminate_zeros()
        return i0, i1, m


# --- readers -------------------------------------------------------------------------------------
class Reader(ABC):
    """Hourly mm on ``grid`` for (run, lead): rain of ``(run + lead - 1 h, run + lead]``."""

    name: str
    cache_write = True  # False: read the decoded-field cache but do not add to it (rain scale's backfill)

    def __init__(self, grid: GridSpec, cache_dir: Path | None = None, keep_days: int = 3, log=None):
        self.grid, self.log = grid, log or (lambda m: None)
        self._leads: dict[datetime, tuple[int, ...]] = {}
        self._mem: dict[tuple, np.ndarray] = {}
        self.cache_dir = None
        if cache_dir is not None:
            g = grid
            self.cache_dir = Path(cache_dir) / self.name / f"{g.name}-{g.nx}x{g.ny}-{g.lon0:g}_{g.lat0:g}"
            self._prune(keep_days)

    # caching
    def _prune(self, keep_days: int) -> None:
        if not self.cache_dir or not self.cache_dir.is_dir():
            return
        cut = datetime.now(UTC) - timedelta(days=keep_days)
        for d in self.cache_dir.iterdir():
            try:
                old = parse_run(d.name) < cut
            except ValueError:
                continue
            if old:
                shutil.rmtree(d, ignore_errors=True)

    def _cached(self, run: datetime, name: str, make) -> np.ndarray:
        key = (run, name)
        if key in self._mem:
            return self._mem[key]
        path = self.cache_dir / f"{run:%Y%m%d%H}" / f"{name}.npy" if self.cache_dir else None
        if path is not None and path.exists():
            a = np.load(path)
            if a.shape == self.grid.shape:
                self._mem[key] = a
                return a
        a = np.asarray(make(), dtype=np.float32)
        if path is not None and self.cache_write:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp.npy")
            np.save(tmp, a)
            os.replace(tmp, path)
        self._mem[key] = a
        return a

    # what a run offers
    def leads(self, run: datetime) -> tuple[int, ...]:
        """Lead hours (>= 1) with an hourly value for this run; () if the run is not published."""
        if run not in self._leads:
            self._leads[run] = self._list_leads(run)
        return self._leads[run]

    @abstractmethod
    def _list_leads(self, run: datetime) -> tuple[int, ...]:
        ...

    @abstractmethod
    def hourly(self, run: datetime, lead: int) -> tuple[np.ndarray, str]:
        """(mm (ny, nx) float32, how it was derived) for the hour ending at ``run + lead``."""


class GribReader(Reader):
    """ECMWF open data ``ifs/0p1/oper`` tp, one GRIB message per step by HTTP range request."""

    name = "ecmwf-s3"

    def __init__(self, grid: GridSpec, bucket: Bucket, native: GridSpec = ECMWF_GLOBAL, **kw):
        super().__init__(grid, **kw)
        self.bucket, self.native = bucket, native
        grid.offset_in(native)  # the window must line up with the native grid
        self._steps: dict[datetime, tuple[int, ...]] = {}

    @staticmethod
    def prefix(run: datetime) -> str:
        return f"{run:%Y%m%d}/{run:%H}z/ifs/0p1/oper/"

    def file(self, run: datetime, step: int) -> str:
        return f"{self.prefix(run)}{run:%Y%m%d%H}0000-{step}h-oper-fc-sfc"

    def steps(self, run: datetime) -> tuple[int, ...]:
        if run not in self._steps:
            try:
                keys = set(self.bucket.list(self.prefix(run)))
            except NotFound:
                keys = set()
            pat = re.compile(re.escape(self.prefix(run)) + rf"{run:%Y%m%d%H}0000-(\d+)h-oper-fc-sfc\.index$")
            st = {int(m.group(1)) for k in keys if (m := pat.match(k))}
            self._steps[run] = tuple(sorted(s for s in st if self.file(run, s) + ".grib2" in keys))
        return self._steps[run]

    def _list_leads(self, run: datetime) -> tuple[int, ...]:
        st = [s for s in self.steps(run) if s > 0]
        out, prev = [], 0
        for s in st:  # every hour up to the last step is covered by some step interval
            out += range(prev + 1, s + 1)
            prev = s
        return tuple(out)

    def interval(self, run: datetime, lead: int) -> tuple[int, int]:
        st = self.steps(run)
        later = [s for s in st if s >= lead]
        if lead < 1 or not later:
            raise ValueError(f"run {run_label(run)} has no step covering lead {lead} h (steps {st})")
        s1 = later[0]
        s0 = max([0] + [s for s in st if s < lead])
        return s0, s1

    def tp(self, run: datetime, step: int) -> np.ndarray:
        """Accumulated mm from the run start to ``step`` on the window."""
        if step == 0:
            return np.zeros(self.grid.shape, dtype=np.float32)

        def fetch():
            base = self.file(run, step)
            entries = [json.loads(x) for x in self.bucket.get(base + ".index").decode().splitlines() if x.strip()]
            hit = [e for e in entries if e.get("param") == "tp" and e.get("levtype", "sfc") == "sfc"
                   and str(e.get("step")) == str(step)]
            if len(hit) != 1:
                raise ValueError(f"{base}.index: {len(hit)} tp entries for step {step}")
            off, n = int(hit[0]["_offset"]), int(hit[0]["_length"])
            mm, info = decode_grib_tp(self.bucket.get(base + ".grib2", off, off + n), self.grid, self.native)
            if (info.run, info.step0, info.step1) != (run, 0, step):
                raise ValueError(f"{base}: message is run {info.run} steps {info.step0}-{info.step1}, expected "
                                 f"{run} 0-{step}")
            return mm
        return self._cached(run, f"tp_{step:03d}", fetch)

    def hourly(self, run: datetime, lead: int) -> tuple[np.ndarray, str]:
        s0, s1 = self.interval(run, lead)
        acc = self.tp(run, s1) - self.tp(run, s0)
        neg = float(np.nanmin(acc))
        if neg < -0.5:
            raise ValueError(f"run {run_label(run)}: tp({s1} h) - tp({s0} h) has {neg:.2f} mm (not accumulated?)")
        return np.maximum(acc, 0, where=np.isfinite(acc), out=acc) / (s1 - s0), f"tp {s0}-{s1} h / {s1 - s0}"


def _child(r, name: str):
    try:
        return r.get_child_by_name(name)
    except (ValueError, KeyError):
        return None


class OpenMeteoReader(Reader):
    """Open-Meteo ``ecmwf_ifs`` (O1280, hourly), ``precipitation`` of the NZ rows, regridded bilinearly."""

    name = "openmeteo-s3"

    def __init__(self, grid: GridSpec, bucket: Bucket, ogrid: OctahedralGrid | None = None,
                 prefix: str = OPENMETEO_PREFIX, **kw):
        super().__init__(grid, **kw)
        self.bucket, self.prefix = bucket, prefix
        self.ogrid = ogrid or OctahedralGrid(1280)
        self.i0, self.i1, self.M = self.ogrid.bilinear(grid)

    def run_prefix(self, run: datetime) -> str:
        return f"{self.prefix}{run:%Y/%m/%d/%H}00Z/"

    def key(self, run: datetime, lead: int) -> str:
        return f"{self.run_prefix(run)}{run + timedelta(hours=lead):%Y-%m-%dT%H%M}.om"

    def _list_leads(self, run: datetime) -> tuple[int, ...]:
        try:
            keys = self.bucket.list(self.run_prefix(run))
        except NotFound:
            return ()
        have = set()
        for k in keys:
            m = re.search(r"/(\d{4})-(\d{2})-(\d{2})T(\d{2})00\.om$", k)
            if m:
                v = datetime(*map(int, m.groups()), tzinfo=UTC)
                have.add(int((v - run) / timedelta(hours=1)))
        return tuple(sorted(L for L in have if L >= 1 and L - 1 in have))  # hourly steps only

    def read(self, run: datetime, lead: int) -> np.ndarray:
        """``precipitation`` on the bracketing O1280 rows (points i0..i1), mm."""
        import omfiles

        key = self.key(run, lead)
        f = RangeFile(self.bucket, key)
        r = omfiles.OmFileReader.from_fsspec(f, key)
        try:
            meta = {}
            for name in ("crs_wkt", "forecast_reference_time", "valid_time"):
                c = _child(r, name)
                meta[name] = c.read_scalar() if c is not None else None
            if meta["crs_wkt"] is not None and f"O{self.ogrid.n}" not in str(meta["crs_wkt"]):
                raise ValueError(f"{key}: grid is not O{self.ogrid.n}")
            for name, want in (("forecast_reference_time", run), ("valid_time", run + timedelta(hours=lead))):
                if meta[name] is not None and int(meta[name]) != int(want.timestamp()):
                    raise ValueError(f"{key}: {name} {meta[name]} != {want.isoformat()}")
            p = _child(r, "precipitation")
            if p is None:
                raise ValueError(f"{key}: no precipitation variable")
            shape = tuple(p.shape)
            if shape[-1] != self.ogrid.size or int(np.prod(shape[:-1])) != 1:
                raise ValueError(f"{key}: precipitation has shape {shape}, O{self.ogrid.n} has {self.ogrid.size}")
            v = p[(0,) * (len(shape) - 1) + (slice(self.i0, self.i1),)] if len(shape) > 1 else p[self.i0:self.i1]
        finally:
            r.close()
        v = np.asarray(v, dtype=np.float64).ravel()
        if np.any(v < -1e-6):
            raise ValueError(f"{key}: negative precipitation")
        return v

    def hourly(self, run: datetime, lead: int) -> tuple[np.ndarray, str]:
        if lead not in self.leads(run):
            raise ValueError(f"run {run_label(run)} has no hourly Open-Meteo file for lead {lead} h")

        def fetch():
            return (self.M @ self.read(run, lead)).reshape(self.grid.shape)
        return self._cached(run, f"precip_{lead:03d}", fetch), "hourly, O1280 bilinear"


def make_reader(name: str, grid: GridSpec, http: Http | None = None, cache_dir: Path | None = None,
                keep_days: int = 3, ecmwf_urls=ECMWF_URLS, openmeteo_url: str = OPENMETEO_URL, log=None) -> Reader:
    http = http or Http(log=log)
    if name == "ecmwf-s3":
        return GribReader(grid, Bucket(list(ecmwf_urls), http), cache_dir=cache_dir, keep_days=keep_days, log=log)
    if name == "openmeteo-s3":
        return OpenMeteoReader(grid, Bucket([openmeteo_url], http), cache_dir=cache_dir, keep_days=keep_days,
                               log=log)
    raise ValueError(f"unknown ECMWF reader {name!r} (known: {', '.join(READERS)})")


# --- which run feeds which hour --------------------------------------------------------------------
Plan = dict[int, tuple[datetime, int]]  # valid hour -> (run, lead)


def obs_plan(reader: Reader, latest: datetime, leads: tuple[int, int], n_runs: int,
             hours: int = SLOTS, now_hour: int | None = None) -> tuple[Plan, list[int], int]:
    """Observed proxy: for each of the ``hours`` ending at the window's last hour, the newest of the
    ``n_runs`` runs up to ``latest`` whose lead for it lies in ``leads`` and is published (a run still
    being converted offers the leads it lists so far; older runs fill the hours after them).

    The last hour is ``now_hour`` (the latest whole hour, so the forecast's first hour follows it
    directly), at most ``latest + leads[1]``; if no run has published it yet, the last hour before it
    that one has (the window then lags "now"). Hours inside the window that no run covers (a run
    missing, a gap in a run being converted) are returned as missing. ``now_hour`` is also needed
    because Open-Meteo publishes a lead a few minutes before that hour ends (the 00z run's 06:00Z file
    was out by 05:50Z on 2026-11-20), and an hour that has not ended yet is not observed (it is the
    forecast's first hour).

    Returns (plan, hours with no run, last hour)."""
    lo, hi = leads
    runs = [latest - timedelta(hours=RUN_EVERY * k) for k in range(n_runs)]

    def pick(h: int) -> tuple[datetime, int] | None:
        for run in runs:  # newest first; a run is listed only when its lead for h is in range
            lead = h - hour_index(run)
            if lo <= lead <= hi and lead in reader.leads(run):
                return run, lead
        return None

    top = hour_index(latest) + hi
    if now_hour is not None:
        top = min(top, int(now_hour))
    end = top
    while end > top - hours and pick(end) is None:  # the newest hours no run has published yet
        end -= 1
    if end == top - hours:  # nothing published in the whole window: keep it at the top, all missing
        end = top
    plan, missing = {}, []
    for h in range(end - hours + 1, end + 1):
        p = pick(h)
        if p is None:
            missing.append(h)
        else:
            plan[h] = p
    return plan, missing, end


def fc_plan(reader: Reader, run: datetime, now_hour: int, hours: int = SLOTS) -> tuple[Plan, list[int]]:
    """Forecast: hours ``now+1 .. now+hours`` from ``run``; (plan, hours the run does not cover)."""
    plan, missing = {}, []
    have = set(reader.leads(run))
    for h in range(now_hour + 1, now_hour + hours + 1):
        lead = h - hour_index(run)
        if lead in have:
            plan[h] = (run, lead)
        else:
            missing.append(h)
    return plan, missing


def find_run(reader: Reader, newest: datetime, need, search_hours: int = 48) -> datetime | None:
    """The newest run at or before ``newest`` (looking back ``search_hours``) whose leads include
    every lead in ``need(run)``."""
    run = floor_run(newest)
    stop = newest - timedelta(hours=search_hours)
    while run >= stop:
        want = need(run)
        if want and set(want) <= set(reader.leads(run)):
            return run
        run -= timedelta(hours=RUN_EVERY)
    return None


class EcmwfSource(RainfallSource):
    """ECMWF hourly fields for the hours of a plan, as ``ECMWF_OBS`` or ``ECMWF_FC``."""

    family = "ecmwf"
    products = ("ECMWF_OBS", "ECMWF_FC")

    def __init__(self, reader: Reader, product_tag: str, plan: Plan):
        if product_tag not in self.products:
            raise ValueError(f"{product_tag} is not an ECMWF product")
        self.reader, self.grid, self.tag, self.plan = reader, reader.grid, product_tag, dict(plan)

    def fields(self, start: datetime, end: datetime) -> Iterator[HourlyField]:
        a, b = hour_index(start), hour_index(end)
        for h in sorted(self.plan):
            if a < h <= b:
                run, lead = self.plan[h]
                mm, how = self.reader.hourly(run, lead)
                yield HourlyField(self.grid, mm, hour_time(h), self.tag, source=self.reader.name, run=run,
                                  lead_hours=lead, extra={"method": how})

    def latest(self) -> datetime | None:
        return hour_time(max(self.plan)) if self.plan else None

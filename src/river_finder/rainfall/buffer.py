"""Rolling 24-slot hourly buffer per catchment, with raw and scaled values and their provenance.

One :class:`HourlyBuffer` holds one metric for every catchment: 24 hourly slots, slot ``h % 24``
for valid hour ``h`` (hours since the epoch, hour ending). Per slot it keeps the valid hour, the
product (code, see :data:`.sources.PRODUCTS`), the source string, run and lead time (forecasts),
what the last write replaced, how often the slot was written and when. Per slot and catchment it
keeps raw mm and scaled mm (float32, NaN = no value). Scale factors are per source family and
scaling zone (rf-t26.5; zones since rf-t26.16, :mod:`.zones`: a network region or part of one), so
they are stored per slot and zone. The buffer's ``regions`` are the zone names and ``region_of`` maps
each catchment to its zone; ``scaled = raw * factor[slot, region_of]``. :meth:`HourlyBuffer.rezone`
moves a buffer to other zones (its hours become unscaled until factors are applied again).

Two kinds:

- ``observed``: the anchor is the latest hour written; the buffer holds hours
  ``anchor-23 .. anchor``. Writing a newer hour moves the anchor and empties slots that fall out.
  :meth:`HourlyBuffer.window` sums the last ``x`` hours (any ``x`` in 1..24).
- ``forecast``: the anchor is "now", set with :meth:`HourlyBuffer.advance`; the buffer holds hours
  ``anchor+1 .. anchor+24``. :meth:`HourlyBuffer.ahead` sums the next ``x`` hours.

Overwrites: for an hour that already has data, a product of higher priority replaces it (NRT over
NOW); for equal priority the later run wins (or the same run re-ingested). A worse product is
skipped. Every replacement is recorded (``replaced``, ``writes``).

Missing data is explicit: a window reports, per catchment, how many of its hours had no value
(``n_missing``), and which hours were missing everywhere; it never quietly sums fewer hours.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .grid import hour_time
from .sources import BY_CODE, product

SLOTS = 24
DEFAULT_WINDOWS = (1, 3, 6, 12, 24)
FORMAT = "river-finder rain buffer v1"


@dataclass
class ScaleFactors:
    """Scale factors of one source family, per region (rf-t26.5). Version 0 = unscaled (factor 1)."""

    family: str
    regions: tuple[str, ...]
    factor: np.ndarray    # float32 (n_regions,)
    version: np.ndarray   # int32 (n_regions,)

    def __post_init__(self):
        self.regions = tuple(self.regions)
        self.factor = np.asarray(self.factor, dtype=np.float32)
        self.version = np.asarray(self.version, dtype=np.int32)
        if self.factor.shape != (len(self.regions),) or self.version.shape != (len(self.regions),):
            raise ValueError("factor and version need one value per region")
        if not np.all(np.isfinite(self.factor) & (self.factor >= 0)):
            raise ValueError("scale factors must be finite and >= 0")

    @staticmethod
    def unity(family: str, regions: Iterable[str]) -> ScaleFactors:
        regions = tuple(regions)
        return ScaleFactors(family, regions, np.ones(len(regions)), np.zeros(len(regions)))


@dataclass
class PutResult:
    hour: int
    product: str
    status: str               # new | replaced | updated | skipped | outside
    previous: str | None = None

    def __str__(self) -> str:
        t = hour_time(self.hour).strftime("%Y-%m-%d %H:00Z")
        return f"{t} {self.product}: {self.status}" + (f" ({self.previous})" if self.previous else "")


@dataclass
class WindowTotal:
    """Rain over ``hours`` consecutive hours, per catchment."""

    hours: int
    first_hour: int
    last_hour: int
    mm: np.ndarray              # float32: sum over the hours that have a value
    n_missing: np.ndarray       # uint8: hours of the window with no value for the catchment
    missing_hours: list[int]    # hours with no data at all (no slot)
    products: dict[str, int]    # product tag -> hours it supplied
    scaled: bool

    @property
    def complete(self) -> np.ndarray:
        return self.n_missing == 0

    @property
    def all_complete(self) -> bool:
        return not self.missing_hours and bool(np.all(self.n_missing == 0))

    def strict(self) -> np.ndarray:
        """``mm`` with NaN for catchments that miss any hour."""
        return np.where(self.complete, self.mm, np.nan).astype(np.float32)

    def m3(self, area_m2: np.ndarray, strict: bool = False) -> np.ndarray:
        """Volume (m³) given each catchment's area: mm / 1000 x area."""
        mm = self.strict() if strict else self.mm
        return mm.astype(np.float64) / 1000.0 * area_m2

    def describe(self) -> str:
        start, end = hour_time(self.first_hour - 1), hour_time(self.last_hour)
        miss = f", {len(self.missing_hours)} hours missing" if self.missing_hours else ""
        part = int(np.count_nonzero(self.n_missing))
        return (f"{self.hours} h {start:%Y-%m-%d %H:00Z} .. {end:%Y-%m-%d %H:00Z}: products {self.products}{miss}, "
                f"{part} catchments incomplete")


class HourlyBuffer:
    def __init__(self, n: int, kind: str = "observed", regions: Iterable[str] = ("",),
                 region_of: np.ndarray | None = None, rows_digest: str = "", slots: int = SLOTS):
        if kind not in ("observed", "forecast"):
            raise ValueError(f"buffer kind must be observed or forecast, not {kind!r}")
        self.kind, self.n, self.slots, self.rows_digest = kind, int(n), int(slots), rows_digest
        self.regions = tuple(regions)
        self.region_of = (np.zeros(n, dtype=np.int16) if region_of is None
                          else np.asarray(region_of, dtype=np.int16))
        if self.region_of.shape != (n,) or (n and self.region_of.max(initial=-1) >= len(self.regions)):
            raise ValueError("region_of needs one region index (or -1) per catchment")
        s, r = self.slots, len(self.regions)
        self.anchor = -1
        self.raw = np.full((s, n), np.nan, dtype=np.float32)
        self.scaled = np.full((s, n), np.nan, dtype=np.float32)
        self.hour = np.full(s, -1, dtype=np.int64)
        self.product = np.zeros(s, dtype=np.uint8)
        self.source = np.full(s, "", dtype="<U32")
        self.run_hour = np.full(s, -1, dtype=np.int64)
        self.lead = np.full(s, -1, dtype=np.int16)
        self.replaced = np.zeros(s, dtype=np.uint8)
        self.writes = np.zeros(s, dtype=np.uint16)
        self.ingested = np.zeros(s, dtype=np.int64)
        self.factor = np.ones((s, r), dtype=np.float32)
        self.factor_version = np.zeros((s, r), dtype=np.int32)

    @classmethod
    def for_weights(cls, weights, kind: str = "observed", zones=None) -> HourlyBuffer:
        """A buffer whose rows are the catchments (rows) of a :class:`.weights.CatchmentWeights`, scaled per
        ``zones`` (a :class:`.zones.ZoneMap`; default: one zone per network region)."""
        if zones is None:
            return cls(weights.n, kind, weights.regions, weights.region_of, weights.rows_digest)
        if zones.rows_digest != weights.rows_digest:
            raise ValueError("the scaling zones are for other rows than the weights")
        return cls(weights.n, kind, zones.names, zones.zone_of, weights.rows_digest)

    def same_zones(self, names, zone_of) -> bool:
        return self.regions == tuple(names) and np.array_equal(self.region_of, np.asarray(zone_of, dtype=np.int16))

    def rezone(self, names, zone_of) -> bool:
        """Scale per other zones: every held hour becomes unscaled (factor 1, version 0) until factors for the new
        zones are applied (:meth:`apply_factors`). Returns False when the zones are the same."""
        names, zone_of = tuple(names), np.asarray(zone_of, dtype=np.int16)
        if self.same_zones(names, zone_of):
            return False
        if zone_of.shape != (self.n,) or (self.n and zone_of.max(initial=-1) >= len(names)):
            raise ValueError("zone_of needs one zone index (or -1) per catchment")
        self.regions, self.region_of = names, zone_of
        self.factor = np.ones((self.slots, len(names)), dtype=np.float32)
        self.factor_version = np.zeros((self.slots, len(names)), dtype=np.int32)
        self.scaled = self.raw.copy()
        return True

    # --- hours held -----------------------------------------------------------------------------
    def span(self) -> tuple[int, int]:
        """First and last hour the buffer can hold at its anchor."""
        if self.kind == "observed":
            return self.anchor - self.slots + 1, self.anchor
        return self.anchor + 1, self.anchor + self.slots

    def has(self, h: int) -> bool:
        return self.anchor >= 0 and self.hour[h % self.slots] == h

    def hours_held(self) -> list[int]:
        return sorted(int(h) for h in self.hour if h >= 0)

    def _clear(self, s: int) -> None:
        self.raw[s] = np.nan
        self.scaled[s] = np.nan
        self.hour[s], self.product[s], self.source[s], self.run_hour[s], self.lead[s] = -1, 0, "", -1, -1
        self.replaced[s], self.writes[s], self.ingested[s] = 0, 0, 0
        self.factor[s], self.factor_version[s] = 1.0, 0

    def advance(self, anchor: int) -> None:
        """Move the anchor forward (observed: latest hour; forecast: "now") and drop hours out of range."""
        if anchor < self.anchor:
            raise ValueError(f"cannot move the {self.kind} buffer back from {self.anchor} to {anchor}")
        self.anchor = int(anchor)
        lo, hi = self.span()
        for s in range(self.slots):
            if self.hour[s] >= 0 and not lo <= self.hour[s] <= hi:
                self._clear(s)

    # --- writing --------------------------------------------------------------------------------
    def put(self, hour: int, raw: np.ndarray, product_tag: str, source: str = "", run_hour: int = -1,
            lead: int = -1, factors: ScaleFactors | None = None, force: bool = False) -> PutResult:
        """Write one hour of raw mm per catchment (NaN = no value), scaling it with ``factors``."""
        p = product(product_tag)
        if p.kind != self.kind:
            raise ValueError(f"{product_tag} is a {p.kind} product; this buffer is {self.kind}")
        raw = np.asarray(raw, dtype=np.float32)
        if raw.shape != (self.n,):
            raise ValueError(f"raw has shape {raw.shape}, buffer has {self.n} catchments")
        f = factors or ScaleFactors.unity(p.family, self.regions)
        self._check_factors(f, p.family)  # validate everything before changing any state
        hour = int(hour)
        if self.kind == "observed" and hour > self.anchor:
            self.advance(hour)
        lo, hi = self.span()
        if self.anchor < 0 or not lo <= hour <= hi:
            return PutResult(hour, product_tag, "outside")
        s = hour % self.slots
        prev = None
        if self.hour[s] == hour:
            old = BY_CODE[int(self.product[s])]
            prev = old.tag
            if not force and (p.priority, run_hour) < (old.priority, int(self.run_hour[s])):
                return PutResult(hour, product_tag, "skipped", prev)
            status = "replaced" if old.code != p.code else "updated"
            self.replaced[s] = old.code
            self.writes[s] += 1
        else:
            self._clear(s)
            status = "new"
            self.writes[s] = 1
        self.raw[s] = raw
        self._scale(s, f)
        self.hour[s], self.product[s], self.source[s] = hour, p.code, source or p.family
        self.run_hour[s], self.lead[s], self.ingested[s] = run_hour, lead, int(time.time())
        return PutResult(hour, product_tag, status, prev)

    def _check_factors(self, f: ScaleFactors, family: str) -> None:
        if f.family != family:
            raise ValueError(f"scale factors are for {f.family}, the product is {family}")
        if f.regions != self.regions:
            raise ValueError("scale factors and buffer list different regions")

    def _scale(self, s: int, f: ScaleFactors) -> None:
        per_catchment = np.append(f.factor, np.float32(1.0))[self.region_of]  # region -1 -> factor 1
        self.scaled[s] = self.raw[s] * per_catchment
        self.factor[s], self.factor_version[s] = f.factor, f.version

    def apply_factors(self, factors: ScaleFactors, hours: Iterable[int] | None = None) -> int:
        """Re-scale the held hours of ``factors.family`` (all, or only ``hours``); returns how many."""
        want = None if hours is None else {int(h) for h in hours}
        done = 0
        for s in range(self.slots):
            if self.hour[s] < 0 or BY_CODE[int(self.product[s])].family != factors.family:
                continue
            if want is not None and int(self.hour[s]) not in want:
                continue
            self._check_factors(factors, factors.family)
            self._scale(s, factors)
            done += 1
        return done

    # --- reading --------------------------------------------------------------------------------
    def total(self, hours: Iterable[int], scaled: bool = True) -> WindowTotal:
        """Sum over the given consecutive hours; hours without data count as missing."""
        hs = [int(h) for h in hours]
        vals = self.scaled if scaled else self.raw
        acc = np.zeros(self.n, dtype=np.float64)
        n_missing = np.zeros(self.n, dtype=np.uint8)
        missing, products = [], {}
        for h in hs:
            if not self.has(h):
                missing.append(h)
                n_missing += 1
                continue
            s = h % self.slots
            v = vals[s]
            nan = np.isnan(v)
            n_missing += nan
            acc += np.where(nan, 0.0, v)
            tag = BY_CODE[int(self.product[s])].tag
            products[tag] = products.get(tag, 0) + 1
        return WindowTotal(len(hs), hs[0], hs[-1], acc.astype(np.float32), n_missing, missing, products, scaled)

    def _check_x(self, x: int) -> int:
        if int(x) != x or not 1 <= x <= self.slots:
            raise ValueError(f"window must be a whole number of hours in 1..{self.slots}, not {x!r}")
        return int(x)

    def window(self, x: int, end: int | None = None, scaled: bool = True) -> WindowTotal:
        """Observed rain over the ``x`` hours ending at ``end`` (default: the latest hour held).

        Pass ``end`` = the current hour to see hours that have not arrived yet as missing.
        """
        if self.kind != "observed":
            raise ValueError("window() is for observed buffers; use ahead() for forecasts")
        x = self._check_x(x)
        end = self.anchor if end is None else int(end)
        return self.total(range(end - x + 1, end + 1), scaled)

    def ahead(self, x: int, start: int | None = None, scaled: bool = True) -> WindowTotal:
        """Forecast rain over the ``x`` hours after ``start`` (default: the anchor, "now")."""
        if self.kind != "forecast":
            raise ValueError("ahead() is for forecast buffers; use window() for observed rain")
        x = self._check_x(x)
        start = self.anchor if start is None else int(start)
        return self.total(range(start + 1, start + x + 1), scaled)

    def windows(self, xs: Iterable[int] = DEFAULT_WINDOWS, scaled: bool = True) -> dict[int, WindowTotal]:
        f = self.window if self.kind == "observed" else self.ahead
        return {int(x): f(x, scaled=scaled) for x in xs}

    def slot_table(self) -> list[dict]:
        """One row per hour in the buffer's span (held or missing), oldest first."""
        rows = []
        if self.anchor < 0:
            return rows
        lo, hi = self.span()
        for h in range(lo, hi + 1):
            s = h % self.slots
            row = {"hour": hour_time(h).strftime("%Y-%m-%d %H:00Z")}
            if self.hour[s] != h:
                rows.append(dict(row, product=None))
                continue
            p = BY_CODE[int(self.product[s])].tag
            rows.append(dict(row, product=p, source=str(self.source[s]),
                             replaced=BY_CODE[int(self.replaced[s])].tag if self.replaced[s] else None,
                             writes=int(self.writes[s]),
                             run=hour_time(self.run_hour[s]).strftime("%Y-%m-%d %H:00Z")
                             if self.run_hour[s] >= 0 else None,
                             lead=int(self.lead[s]) if self.lead[s] >= 0 else None,
                             factor_versions=sorted({int(v) for v in self.factor_version[s]}),
                             catchments_without_value=int(np.isnan(self.raw[s]).sum())))
        return rows

    # --- files ----------------------------------------------------------------------------------
    def check_rows(self, weights) -> None:
        if self.rows_digest and weights.rows_digest != self.rows_digest:
            raise ValueError("buffer rows do not match the weight matrix rows (different catchment table)")

    def save(self, path: Path, compress: bool = True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"format": FORMAT, "kind": self.kind, "n": self.n, "slots": self.slots, "anchor": self.anchor,
                "rows_digest": self.rows_digest, "regions": list(self.regions), "saved": int(time.time())}
        arrays = {k: getattr(self, k) for k in ("raw", "scaled", "hour", "product", "source", "run_hour", "lead",
                                                "replaced", "writes", "ingested", "factor", "factor_version",
                                                "region_of")}
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            (np.savez_compressed if compress else np.savez)(fh, meta=np.array(json.dumps(meta)), **arrays)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> HourlyBuffer:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != FORMAT:
                raise ValueError(f"{path}: not a {FORMAT} file")
            b = cls(meta["n"], meta["kind"], meta["regions"], z["region_of"], meta["rows_digest"], meta["slots"])
            b.anchor = int(meta["anchor"])
            for k in ("raw", "scaled", "hour", "product", "source", "run_hour", "lead", "replaced", "writes",
                      "ingested", "factor", "factor_version"):
                setattr(b, k, z[k].astype(getattr(b, k).dtype))
        return b

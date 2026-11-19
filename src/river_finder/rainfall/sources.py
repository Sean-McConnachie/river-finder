"""The ``RainfallSource`` interface: every product yields hourly mm grids tagged with what they are.

A source belongs to a *family* (``gsmap``, ``ecmwf``): scale factors are per family and region, and
each family has its own grid (:mod:`.grid`). Within a family a source yields one or more
*products*; each hourly field carries its product tag, so the buffer records which product filled
each hour and lets a better product replace a worse one for the same hour (``priority``).

Real sources come in later children: GSMaP NOW/NRT (rf-t26.2) and ECMWF IFS observed/forecast
(rf-t26.3). :class:`SyntheticSource` is for tests and dry runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .grid import GridSpec, hour_index, hour_time


@dataclass(frozen=True)
class Product:
    tag: str
    code: int        # stored per buffer slot (uint8); 0 means empty
    family: str      # source family: scale factors and grids are per family
    kind: str        # "observed" (past hours) or "forecast" (hours ahead)
    priority: int    # for the same hour, a product of higher priority replaces a lower one
    note: str = ""


PRODUCTS: dict[str, Product] = {p.tag: p for p in (
    Product("GSMAP_NOW", 1, "gsmap", "observed", 20, "JAXA GSMaP_NOW, real time, updated every 30 min"),
    Product("GSMAP_NRT", 2, "gsmap", "observed", 30, "JAXA GSMaP_NRT, ~4 h latency, replaces NOW"),
    Product("ECMWF_OBS", 10, "ecmwf", "observed", 10, "ECMWF IFS short-lead hours used as an observed proxy"),
    Product("ECMWF_FC", 11, "ecmwf", "forecast", 10, "ECMWF IFS forecast hours (rain in the next x hours)"),
    Product("SYNTHETIC", 250, "synthetic", "observed", 10, "tests"),
    Product("SYNTHETIC_BETTER", 251, "synthetic", "observed", 15, "tests: replaces SYNTHETIC"),
    Product("SYNTHETIC_FC", 252, "synthetic", "forecast", 10, "tests"),
)}
BY_CODE: dict[int, Product] = {p.code: p for p in PRODUCTS.values()}


def product(tag: str) -> Product:
    try:
        return PRODUCTS[tag]
    except KeyError:
        raise ValueError(f"unknown rainfall product {tag!r} (known: {', '.join(PRODUCTS)})") from None


def family_products(family: str, kind: str | None = None) -> list[str]:
    return [p.tag for p in PRODUCTS.values() if p.family == family and (kind is None or p.kind == kind)]


@dataclass(frozen=True)
class HourlyField:
    """Rain that fell in one hour on one grid.

    ``valid`` is the *end* of the hour (UTC, on the hour): the field holds the rain of
    ``(valid - 1 h, valid]``. ``mm`` is (ny, nx) float32, NaN where the product has no value.
    Forecast-type fields (ECMWF) also carry the run (issue) time and the lead time in hours
    (``valid - run``); for observed products they are ``None``.
    """

    grid: GridSpec
    mm: np.ndarray
    valid: datetime
    product: str
    source: str = ""                 # where the data came from, e.g. "jaxa-ftp", "ecmwf-s3", "openmeteo-s3"
    run: datetime | None = None
    lead_hours: int | None = None
    extra: dict = field(default_factory=dict, compare=False)  # anything the ingester wants to keep

    def __post_init__(self):
        p = product(self.product)
        mm = np.array(self.mm, dtype=np.float32)  # a copy: tiny negatives are clipped below
        if mm.shape != self.grid.shape:
            raise ValueError(f"{self.product}: field shape {mm.shape} != grid {self.grid.name} {self.grid.shape}")
        if np.any(mm < -1e-6):
            raise ValueError(f"{self.product}: negative rain; sources must turn missing-value flags into NaN")
        object.__setattr__(self, "mm", np.maximum(mm, 0, where=np.isfinite(mm), out=mm))
        hour_index(self.valid)  # timezone-aware, whole hour
        if self.run is not None:
            lead = (self.valid - self.run) / timedelta(hours=1)
            if lead != int(lead) or lead < 1:
                raise ValueError(f"{self.product}: valid {self.valid} is not a whole number of hours (>= 1) "
                                 f"after run {self.run}")
            if self.lead_hours is not None and self.lead_hours != int(lead):
                raise ValueError(f"{self.product}: lead_hours {self.lead_hours} != valid - run ({int(lead)} h)")
            object.__setattr__(self, "lead_hours", int(lead))
        if not self.source:
            object.__setattr__(self, "source", p.family)

    @property
    def info(self) -> Product:
        return PRODUCTS[self.product]

    @property
    def hour(self) -> int:
        return hour_index(self.valid)

    @property
    def run_hour(self) -> int:
        return hour_index(self.run) if self.run is not None else -1


class RainfallSource(ABC):
    """One rainfall product family behind a common interface.

    Subclasses set ``family`` (scale factors and grids are per family), ``grid`` (the NZ window the
    fields are on, from ``config/rainfall.toml``) and ``products`` (the tags they may yield).
    """

    family: str
    grid: GridSpec
    products: tuple[str, ...]

    @abstractmethod
    def fields(self, start: datetime, end: datetime) -> Iterator[HourlyField]:
        """Hourly fields whose valid hour (hour ending) lies in ``(start, end]``, oldest hour first.

        Yield every product available for an hour, worse first (NOW, then NRT); the buffer keeps the
        best by priority. Do not yield anything for an hour with no data: the buffer then reports
        it as missing. Forecast sources yield the hours of their latest run.
        """

    def latest(self) -> datetime | None:
        """The latest valid hour the source can deliver now (None if unknown)."""
        return None


class SyntheticSource(RainfallSource):
    """Deterministic rain for tests: ``rain(hour_index) -> (ny, nx)`` mm, or a constant.

    ``missing`` lists hours (``hour_index``) for which nothing is yielded. With ``run`` set, fields
    carry it as their run time (forecast products).
    """

    def __init__(self, grid: GridSpec, rain: float | Callable[[int], np.ndarray] = 1.0,
                 product_tag: str = "SYNTHETIC", missing: set[int] | None = None, run: datetime | None = None,
                 source: str = "synthetic"):
        p = product(product_tag)
        self.family, self.grid, self.products = p.family, grid, (product_tag,)
        self.rain, self.tag, self.missing, self.run, self.source = rain, product_tag, set(missing or ()), run, source

    def fields(self, start: datetime, end: datetime) -> Iterator[HourlyField]:
        for h in range(hour_index(start) + 1, hour_index(end) + 1):
            if h in self.missing:
                continue
            if callable(self.rain):
                mm = np.asarray(self.rain(h), dtype=np.float32)
            else:
                mm = np.full(self.grid.shape, self.rain, dtype=np.float32)
            yield HourlyField(self.grid, mm, hour_time(h), self.tag, source=self.source, run=self.run)

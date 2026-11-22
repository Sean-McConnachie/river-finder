"""Upstream rainfall totals down the river network (rf-t26.6).

**What.** For every reach (row of ``segments_table.pkl``) and every window ``x`` (observed: the past
``x`` hours; forecast: the next ``x`` hours; any whole ``x`` in 1..24)::

    m3_up[r] = sum of local m3[c] over every local catchment c upstream of r, r's own included
    A_up[r]  = sum of the local pixel areas area_m2[c] over the same catchments
    mm_up[r] = m3_up[r] / A_up[r] * 1000

with ``m3[c] = mm[c] / 1000 * area_m2[c]``: ``mm[c]`` is the window sum of the hourly buffer
(:class:`.buffer.HourlyBuffer`, the same numbers as ``window(x)`` / ``ahead(x)``; hours without a
value count 0 and are flagged) and ``area_m2`` the pixel area of each local catchment
(:class:`.weights.CatchmentWeights`; it does not depend on the grid). ``A_up`` is the pixel area, not
the table's ``area_km2``, so ``mm_up`` is the mean depth over exactly the ground that was counted. A
reach whose upstream holds no pixel at all (fallback rows, none today) takes its own local depth.

**Method.** ``down`` (row of the next reach downstream, -1 at an outlet) makes the network a forest.
:func:`topo_order` (Kahn's algorithm, numba, 15 ms for 1.1 M rows) lists every row before the row it
drains into; a row missing from the order sits on a cycle, which raises. :func:`accumulate` runs one
numba pass per quantity in that order, adding each row's value into its downstream row (sum; max for
the missing hours, bitwise or for the bit fields). The quantities are the rows of one ``(k, n)`` array:
volume and missing area-hours per kind and window. Nationally (5 windows x 2 kinds) the accumulation
takes ~0.2 s; the whole computation ~1.5 s after the buffers are read (~1 s), saving ~2.5 s.

**Flags per reach and window** (:class:`KindTotals`; arrays ``(windows, n)``):

- ``miss_hours`` (uint8): the most hours missing from any one catchment upstream (max-accumulated);
  0 = every catchment upstream has every hour of the window.
- ``miss_frac`` (float32): share of the upstream area-hours with no value,
  ``sum(area_c * n_missing_c) / (x * A_up)``. The total covers ``1 - miss_frac`` of the catchment's
  area x time; ``mm / (1 - miss_frac)`` is a first guess at the full depth.
- ``products`` (uint8 bits): bit ``k`` set when product ``product_tags[k]`` supplied a value for the
  window to at least one catchment upstream (OR-accumulated). A buffer slot holds one product for
  every catchment, so the hours each product supplied are the same everywhere and are kept per window
  in the summary; the bits only drop a product whose hours had no value anywhere upstream.
- ``scale`` (uint8 bits): ``RAW`` (1) = some catchment-hour upstream used factor version 0 (unscaled),
  ``SCALED`` (2) = some used a version > 0 (gauge-scaled, rf-t26.5); 3 = mixed, 0 = no value. The
  exact versions per hour and region are in the slot arrays; with ``up_regions`` (a bit per region
  upstream) :meth:`UpstreamTotals.provenance` lists every hour's product, run, lead and the factor
  versions that reached a reach. Totals sum the scaled values unless computed with ``scaled=False``
  (then every value counts as raw). ``scaled`` in the metadata (per kind and overall) is true only when
  some reach has the ``SCALED`` bit, i.e. a factor version > 0 was applied: with the unity factors of
  today (all version 0, rf-t26.5 open) it is false; ``values`` says which array was summed.

Per window the metadata also keeps the hours, missing hours, products, sources, runs, leads and
factor versions, the reaches incomplete, the largest depth and volume and two checks: every reach
equals its tributaries plus its own local volume, and the outlets hold all local volume.

**Network flags per reach** (``flags``, uint8; :class:`Network`):

- ``OUTLET`` (1): ``down < 0`` (6,824 nationally).
- ``EDGE`` (2): an outlet that leaves its tile at a tile edge and that the relink (rf-t26.8,
  ``relink_log.csv``, status other than ``linked``) could not reconnect: the river goes on in the
  next tile, but its totals stop here. 175 outlets holding 34,564 km² of pixels.
- ``EDGE_BIG`` (4): the ``EDGE`` outlets over 200 km², the 26 big rivers of rf-t26.11
  (``unresolved_gt200.csv``): the upper Whanganui, the Tongariro/Taupo system, the Ruamahanga, lakes
  Hawea, Wakatipu and Te Anau split by tiles. The rivers below them miss that water: the Clutha
  mouth counts 18,924 km² of pixels for 20,904 km² in the table, without Lake Hawea (1,304 km²,
  outlet 503016542) and part of Wakatipu (587 km², 403018735).
- ``ABOVE_EDGE`` (8): the reach drains to an ``EDGE`` outlet (its own total is complete as far as it
  goes; it is the rivers below the edge that lack it).
- ``AREA_SHORT`` (16): ``A_up`` is short of the table's ``area_km2`` by at least 5 % and 1 km²: the
  tile added REC2 inflow area at an edge whose pixels drain to an ``EDGE`` outlet in another tile.
  Such a reach's ``mm_up`` is the depth over the part that was counted; ``m3_up`` misses the rest.

**Consistency** (:meth:`Network.area_check`, in ``totals.json``): ``A_up`` against the table's
``area_km2`` by size class. The median ratio is 0.999-1.000 in every class; 92-96 % of reaches below
1,000 km² are within 5 %, but only 64 % of those above (the big rivers cross tile edges). At the
outlets the table holds 278,599 km², the pixels 257,985 km²: 9,491 km² of land pixels drain into no
kept reach (``land_unassigned`` of the weights build), and the other ~11,100 km² of table area lies
beyond the land pixels: mostly REC2 inflow counted twice, in the tile that added it at its edge and at
the unresolved outlet whose pixels it stands for. The
pixel area can also exceed the table's: the Grey's last reaches carry 3,927 km² in the table but
4,370 km² of pixels drain to them (the downstream tile's routing lost part of the inflow, the
relinked pixels did not).

**Files** (in ``<rain>``):

- ``network.npz``: the :class:`Network` (gid, down, vi, regions, local and upstream pixel areas,
  flags, topological order), built from ``segments_table.pkl``, the weights and the relink logs, and
  rebuilt when the table changes (size or mtime). An hourly job can run from it without the 300 MB
  table.
- ``totals.npz`` (+ ``totals.json``): :class:`UpstreamTotals`, a self-contained snapshot: the
  per-reach network fields (gid, down, vi, region, upstream pixel and table areas, flags,
  up_regions) plus, per kind (``obs_*``, ``fc_*``), ``mm``, ``miss_hours``, ``miss_frac``,
  ``products`` and ``scale`` per window and the slot provenance. ``m3`` is derived as
  ``mm / 1000 * up_area_m2``. The ``.json`` holds the windows, the sources, runs and products per
  window, the window ends and their age, the checks, timings and the creation time.

**API**::

    t = UpstreamTotals.load(cfg.totals_path)
    t.mm("observed", 24), t.m3("forecast", 6)       # arrays over all reaches
    t.at(row)                                       # dict: mm, m3 and flags per kind and window
    t.for_gids([gid, ...])                          # DataFrame, one row per gid
    for region, rows in t.viewer_rows().items():    # viewer reaches (vi >= 0) of each region, in vi order
        df = t.frame(rows)                          # row j of df is the reach with vi == j

``river-finder rain totals [--windows ...] [--raw]`` recomputes ``totals.npz`` from the current
buffers.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from .buffer import SLOTS, HourlyBuffer
from .grid import hour_time
from .sources import BY_CODE

FORMAT = "river-finder rain totals v1"
NET_FORMAT = "river-finder rain network v1"
KINDS = ("observed", "forecast")
PREFIX = {"observed": "obs", "forecast": "fc"}

OUTLET, EDGE, EDGE_BIG, ABOVE_EDGE, AREA_SHORT = 1, 2, 4, 8, 16
FLAG_NAMES = {OUTLET: "outlet", EDGE: "tile_edge_outlet", EDGE_BIG: "tile_edge_outlet_gt200km2",
              ABOVE_EDGE: "drains_to_tile_edge", AREA_SHORT: "area_short"}
FLAG_DOC = {
    "outlet": "down < 0: the network ends here",
    "tile_edge_outlet": "outlet at a tile edge that the relink (rf-t26.8) could not reconnect: totals stop here",
    "tile_edge_outlet_gt200km2": "tile-edge outlet over 200 km2, one of the 26 rivers of rf-t26.11",
    "drains_to_tile_edge": "the reach drains to a tile-edge outlet",
    "area_short": "upstream pixel area >= 5 % and >= 1 km2 short of the table's area_km2: the total misses part "
                  "of the catchment (tile-edge losses)",
}
EDGE_BIG_KM2 = 200.0
SHORT_FRAC, SHORT_KM2 = 0.05, 1.0
RAW, SCALED = 1, 2
SCALE_NAMES = {0: "none", RAW: "raw", SCALED: "scaled", RAW | SCALED: "mixed"}


# --- graph kernels ------------------------------------------------------------------------------
@njit(cache=True)
def _topo_order(down):  # pragma: no cover - compiled
    """Kahn's algorithm: rows with nothing upstream first, each row before the row it drains into."""
    n = down.size
    indeg = np.zeros(n, np.int32)
    for i in range(n):
        d = down[i]
        if d >= 0:
            indeg[d] += 1
    order = np.empty(n, np.int32)
    tail = 0
    for i in range(n):
        if indeg[i] == 0:
            order[tail] = i
            tail += 1
    head = 0
    while head < tail:
        i = order[head]
        head += 1
        d = down[i]
        if d >= 0:
            indeg[d] -= 1
            if indeg[d] == 0:
                order[tail] = d
                tail += 1
    return order[:tail]


# Accumulation kernels on a (k, n) array: one pass per row j over the order; ``down_o = down[order]``
# makes the downstream look-ups sequential. 20 float64 rows of 1.1 M reaches take ~0.1 s.
@njit(cache=True)
def _acc_sum(order, down_o, a):  # pragma: no cover - compiled
    for j in range(a.shape[0]):
        row = a[j]
        for t in range(order.size):
            d = down_o[t]
            if d >= 0:
                row[d] += row[order[t]]


@njit(cache=True)
def _acc_max(order, down_o, a):  # pragma: no cover - compiled
    for j in range(a.shape[0]):
        row = a[j]
        for t in range(order.size):
            d = down_o[t]
            if d >= 0:
                v = row[order[t]]
                if v > row[d]:
                    row[d] = v


@njit(cache=True)
def _acc_or(order, down_o, a):  # pragma: no cover - compiled
    for j in range(a.shape[0]):
        row = a[j]
        for t in range(order.size):
            d = down_o[t]
            if d >= 0:
                row[d] |= row[order[t]]


@njit(cache=True)
def _outlet_of(order, down):  # pragma: no cover - compiled
    """Row of the outlet each row drains to (downstream rows first: the order reversed)."""
    out = np.empty(down.size, np.int32)
    for k in range(order.size - 1, -1, -1):
        i = order[k]
        d = down[i]
        out[i] = i if d < 0 else out[d]
    return out


def _check_down(down: np.ndarray) -> np.ndarray:
    down = np.asarray(down, dtype=np.int64)
    if down.ndim != 1 or (down.size and (down.min() < -1 or down.max() >= down.size)):
        raise ValueError("down must hold row indices (or -1) of the same table")
    return down


def topo_order(down: np.ndarray) -> np.ndarray:
    """Rows upstream first (int32). Raises if ``down`` has a cycle."""
    down = _check_down(down)
    order = _topo_order(down)
    if order.size != down.size:
        raise ValueError(f"the network has a cycle: {down.size - order.size} rows never reach an outlet")
    return order


def accumulate(order: np.ndarray, down: np.ndarray, a: np.ndarray, how: str = "sum") -> np.ndarray:
    """Upstream accumulation of ``a`` (``(n,)`` or ``(k, n)``, one row per quantity) in place, in
    topological ``order``: each row's value goes into its downstream row (``how``: sum, max or bitwise or)."""
    two = a if a.ndim == 2 else a.reshape(1, -1)
    if not two.flags.c_contiguous or two.shape[1] != down.size:
        raise ValueError("accumulate needs a C-contiguous (k, n) array, n = rows of down")
    {"sum": _acc_sum, "max": _acc_max, "or": _acc_or}[how](order, down[order].astype(np.int32), two)
    return a


def _digest(a: np.ndarray, dtype) -> str:
    return hashlib.sha1(np.ascontiguousarray(a, dtype=dtype).tobytes()).hexdigest()[:16]


def _region_bits(region_of: np.ndarray, n_regions: int) -> np.ndarray:
    if n_regions > 64:
        raise ValueError(f"{n_regions} regions: up_regions holds at most 64")
    dt = np.uint32 if n_regions <= 32 else np.uint64
    one = dt(1)
    bits = np.where(region_of >= 0, np.left_shift(one, np.maximum(region_of, 0).astype(dt)), dt(0)).astype(dt)
    return bits


# --- network ------------------------------------------------------------------------------------
@dataclass
class Network:
    """The reach network the totals run on: one row per reach, in ``segments_table.pkl`` order."""

    gid: np.ndarray             # int64
    down: np.ndarray            # int64 row downstream, -1 at an outlet
    area_m2: np.ndarray         # float64 local pixel area (weights npix x pixel area)
    region_of: np.ndarray       # int16 region index (-1 none)
    regions: tuple[str, ...]
    vi: np.ndarray              # int32 viewer index within the region (-1: not drawn)
    table_area_km2: np.ndarray  # float32 upstream area of the segment table
    flags: np.ndarray           # uint8 OUTLET | EDGE | EDGE_BIG | ABOVE_EDGE | AREA_SHORT
    order: np.ndarray           # int32 topological order, upstream first
    up_area_m2: np.ndarray      # float64 upstream pixel area
    up_regions: np.ndarray      # uint32/64 bit per region upstream
    meta: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.gid.size)

    @property
    def rows_digest(self) -> str:
        """Same digest as ``CatchmentWeights.rows_digest`` and the buffers' (gid order)."""
        return _digest(self.gid, np.int64)

    @property
    def down_digest(self) -> str:
        return _digest(self.down, np.int64)

    @classmethod
    def build(cls, gid, down, area_m2, region_of, regions, table_area_km2, vi=None, edge_gids=(),
              edge_big_gids=None, meta: dict | None = None) -> Network:
        """A network from arrays. ``edge_gids``: tile-edge outlets the relink could not reconnect;
        ``edge_big_gids``: rf-t26.11's list (default: the ``edge_gids`` over 200 km²)."""
        gid = np.asarray(gid, dtype=np.int64)
        n = gid.size
        down = _check_down(down)
        if down.size != n:
            raise ValueError("down and gid differ in length")
        area_m2 = np.asarray(area_m2, dtype=np.float64)
        region_of = np.asarray(region_of, dtype=np.int16)
        table_area_km2 = np.asarray(table_area_km2, dtype=np.float32)
        vi = np.full(n, -1, dtype=np.int32) if vi is None else np.asarray(vi, dtype=np.int32)
        order = topo_order(down)
        up_area = accumulate(order, down, area_m2.copy())
        up_regions = accumulate(order, down, _region_bits(region_of, len(regions)), "or")
        flags = np.zeros(n, dtype=np.uint8)
        outlet = down < 0
        flags[outlet] |= OUTLET
        edge = outlet & np.isin(gid, np.asarray(list(edge_gids), dtype=np.int64))
        flags[edge] |= EDGE
        if edge_big_gids is None:
            big = edge & (table_area_km2 > EDGE_BIG_KM2)
        else:
            big = edge & np.isin(gid, np.asarray(list(edge_big_gids), dtype=np.int64))
        flags[big] |= EDGE_BIG
        root = _outlet_of(order, down)
        flags[(flags[root] & EDGE) > 0] |= ABOVE_EDGE
        short = table_area_km2.astype(np.float64) - up_area / 1e6
        flags[(short >= SHORT_FRAC * table_area_km2) & (short >= SHORT_KM2)] |= AREA_SHORT
        net = cls(gid, down, area_m2, region_of, tuple(regions), vi, table_area_km2, flags, order, up_area,
                  up_regions, dict(meta or {}))
        net.meta.update(built=datetime.now(UTC).isoformat(timespec="seconds"), rows=n,
                        rows_digest=net.rows_digest, down_digest=net.down_digest, flags=net.flag_counts(),
                        area_check=net.area_check())
        return net

    def flag_counts(self) -> dict[str, int]:
        return {name: int(np.count_nonzero(self.flags & bit)) for bit, name in FLAG_NAMES.items()}

    def area_check(self) -> dict:
        """Upstream pixel area against the table's ``area_km2``: ratio quantiles by size class and outlet totals."""
        tab = self.table_area_km2.astype(np.float64)
        up = self.up_area_m2 / 1e6
        ok = tab > 0
        ratio = np.where(ok, up / np.where(ok, tab, 1.0), np.nan)
        classes = {}
        for lo, hi in ((0, 1), (1, 10), (10, 100), (100, 1000), (1000, np.inf)):
            m = ok & (tab >= lo) & (tab < hi)
            if not m.any():
                continue
            r = ratio[m]
            e = np.abs(r - 1)
            q = np.quantile(r, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
            classes[f"{lo:g}-{hi:g} km2"] = {
                "reaches": int(m.sum()), "ratio_q01_q05_q25_q50_q75_q95_q99": [round(float(v), 4) for v in q],
                "within_1pct": round(float((e <= 0.01).mean()), 4), "within_5pct": round(float((e <= 0.05).mean()), 4),
                "within_10pct": round(float((e <= 0.10).mean()), 4),
                "short_gt10pct": int((r < 0.9).sum()), "over_gt10pct": int((r > 1.1).sum())}
        out = (self.flags & OUTLET) > 0
        edge = (self.flags & EDGE) > 0
        return {
            "by_size": classes,
            "pixel_km2_total": round(float(up[out].sum()), 1),
            "table_km2_at_outlets": round(float(tab[out].sum()), 1),
            "edge_outlets": int(edge.sum()), "edge_outlets_pixel_km2": round(float(up[edge].sum()), 1),
            "edge_outlets_table_km2": round(float(tab[edge].sum()), 1),
            "area_short_reaches": int(np.count_nonzero(self.flags & AREA_SHORT)),
            "area_short_reaches_gt100km2": int(np.count_nonzero(((self.flags & AREA_SHORT) > 0) & (tab > 100))),
            "note": ("pixel areas count each 8 m pixel once, in the tile whose core holds it; the table's area_km2 "
                     "also counts the REC2 inflow a tile adds at its edge. Reaches below an inflow whose pixels "
                     "drain to an unresolved tile-edge outlet (flag tile_edge_outlet; rf-t26.11 for the 26 over "
                     "200 km2) are short by that outlet's area, and land pixels that drain into no kept reach "
                     "(weights build: land_unassigned) are in no local catchment."),
        }

    # --- files ----------------------------------------------------------------------------------
    _ARRAYS = ("gid", "down", "area_m2", "region_of", "vi", "table_area_km2", "flags", "order", "up_area_m2",
               "up_regions")

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta, format=NET_FORMAT, regions=list(self.regions))
        arrays = {k: getattr(self, k) for k in self._ARRAYS}
        arrays["down"] = arrays["down"].astype(np.int32)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, meta=np.array(json.dumps(meta)), **arrays)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> Network:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != NET_FORMAT:
                raise ValueError(f"{path}: not a {NET_FORMAT} file")
            a = {k: z[k] for k in cls._ARRAYS}
        a["down"] = a["down"].astype(np.int64)
        return cls(**a, regions=tuple(meta["regions"]), meta=meta)


def read_weights_network(path: Path) -> dict:
    """The network arrays of a weights file (gid, down, npix, regions, table areas) without the matrix."""
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        out = {k: z[k] for k in ("gid", "down", "npix", "region_of", "table_area_km2")}
    out["regions"] = tuple(meta["regions"])
    out["pixel_area_m2"] = float(meta["pixel_area_m2"])
    out["land_unassigned_km2"] = round(meta.get("build", {}).get("land_unassigned", 0) * out["pixel_area_m2"] / 1e6, 1)
    return out


def network_from_files(weights_path: Path, table_path: Path, relink_log: Path | None = None,
                       unresolved: Path | None = None, log=print) -> Network:
    """Build the :class:`Network` from a weights file, the segment table and the relink logs."""
    w = read_weights_network(weights_path)
    t = pd.read_pickle(table_path)
    if "vi" not in t:
        t = t.assign(vi=-1)
    gid = t["gid"].to_numpy(np.int64)
    if not np.array_equal(gid, w["gid"]):
        raise ValueError(f"{weights_path} rows differ from {table_path}: rebuild the weights (rain weights)")
    down = t["down"].to_numpy(np.int64)
    if not np.array_equal(down, w["down"]):
        log(f"note: {weights_path.name} holds an older down link; using {table_path.name}'s "
            f"({int((down != w['down']).sum())} links differ; rain weights refreshes the copy)")
    edge, big, src = [], None, {}
    if relink_log is not None and Path(relink_log).exists():
        lg = pd.read_csv(relink_log, usecols=["gid", "status"])
        unlinked = lg[lg["status"] != "linked"]
        edge = unlinked["gid"].to_numpy(np.int64)
        src["relink_log"] = {"path": str(relink_log), "outlets_logged": int(len(lg)),
                             "not_linked_by_status": {str(k): int(v) for k, v in
                                                      unlinked["status"].value_counts().items()}}
    if unresolved is not None and Path(unresolved).exists():
        big = pd.read_csv(unresolved, usecols=["gid"])["gid"].to_numpy(np.int64)
        src["unresolved"] = {"path": str(unresolved), "outlets": int(big.size)}
    st = Path(table_path).stat()
    meta = {"table": str(table_path), "table_mtime": st.st_mtime, "table_size": st.st_size,
            "weights": str(weights_path), "pixel_area_m2": w["pixel_area_m2"],
            "land_unassigned_km2": w["land_unassigned_km2"], "sources": src}
    return Network.build(gid, down, w["npix"] * w["pixel_area_m2"], w["region_of"], w["regions"],
                         t["area_km2"].to_numpy(np.float32), t["vi"].to_numpy(np.int32), edge, big, meta)


# --- local windows -------------------------------------------------------------------------------
def _hour(h: int) -> str:
    return hour_time(int(h)).strftime("%Y-%m-%d %H:00Z")


def _span_hours(buf: HourlyBuffer, xmax: int, anchor: int | None) -> list[int]:
    """Hours of the longest window, nearest to the anchor first: observed ``anchor, anchor-1, ...``;
    forecast ``anchor+1, anchor+2, ...`` (default anchor: the buffer's)."""
    a = buf.anchor if anchor is None else int(anchor)
    return [a - k for k in range(xmax)] if buf.kind == "observed" else [a + 1 + k for k in range(xmax)]


@dataclass
class _Local:
    hours: list[int]
    m3: np.ndarray         # float64 (W, n)
    mm: np.ndarray         # float32 (W, n) local window mm (as HourlyBuffer.window)
    n_missing: np.ndarray  # uint8 (W, n)
    products: np.ndarray   # uint8 (W, n)
    scale: np.ndarray      # uint8 (W, n)
    tags: tuple[str, ...]


def _local_windows(buf: HourlyBuffer, area_m2: np.ndarray, windows: tuple[int, ...], scaled: bool,
                   anchor: int | None) -> _Local:
    """Window sums per local catchment for every window in one pass over the hours (nearest first)."""
    n, xs = buf.n, windows
    hours = _span_hours(buf, xs[-1], anchor)
    held = [h for h in hours if buf.has(h)]
    codes = sorted({int(buf.product[h % buf.slots]) for h in held})
    if len(codes) > 8:
        raise ValueError(f"{len(codes)} products in one buffer: the products bit field holds 8")
    bit = {c: np.uint8(1 << k) for k, c in enumerate(codes)}
    vals = buf.scaled if scaled else buf.raw
    ridx = np.where(buf.region_of >= 0, buf.region_of, len(buf.regions)).astype(np.intp)
    W = len(xs)
    out = _Local(hours, np.zeros((W, n)), np.zeros((W, n), np.float32), np.zeros((W, n), np.uint8),
                 np.zeros((W, n), np.uint8), np.zeros((W, n), np.uint8), tuple(BY_CODE[c].tag for c in codes))
    acc = np.zeros(n, dtype=np.float64)
    nmiss = np.zeros(n, dtype=np.uint8)
    prod = np.zeros(n, dtype=np.uint8)
    scl = np.zeros(n, dtype=np.uint8)
    want = {x: i for i, x in enumerate(xs)}
    for k, h in enumerate(hours):
        if buf.has(h):
            s = h % buf.slots
            v = vals[s]
            nan = np.isnan(v)
            acc += np.where(nan, 0.0, v)
            nmiss += nan
            ok = ~nan
            prod[ok] |= bit[int(buf.product[s])]
            if scaled:
                fv = np.append(buf.factor_version[s], 0)[ridx]
                scl |= np.where(ok, np.where(fv > 0, SCALED, RAW), 0).astype(np.uint8)
            else:
                scl[ok] |= RAW
        else:
            nmiss += 1
        i = want.get(k + 1)
        if i is not None:
            mm = acc.astype(np.float32)
            out.mm[i], out.n_missing[i], out.products[i], out.scale[i] = mm, nmiss, prod, scl
            out.m3[i] = mm.astype(np.float64) / 1000.0 * area_m2
    return out


def _slots(buf: HourlyBuffer, hours: list[int]) -> dict[str, np.ndarray]:
    """Provenance of the hours of the longest window, oldest first: product, source, run, lead, factors."""
    hs = sorted(hours)
    r = len(buf.regions)
    out = {"hour": np.array(hs, dtype=np.int64), "product": np.zeros(len(hs), np.uint8),
           "source": np.full(len(hs), "", dtype="<U32"), "run_hour": np.full(len(hs), -1, np.int64),
           "lead": np.full(len(hs), -1, np.int16), "factor": np.ones((len(hs), r), np.float32),
           "factor_version": np.zeros((len(hs), r), np.int32), "without_value": np.zeros(len(hs), np.int64)}
    for i, h in enumerate(hs):
        if not buf.has(h):
            out["without_value"][i] = buf.n
            continue
        s = h % buf.slots
        out["product"][i], out["source"][i] = buf.product[s], buf.source[s]
        out["run_hour"][i], out["lead"][i] = buf.run_hour[s], buf.lead[s]
        out["factor"][i], out["factor_version"][i] = buf.factor[s], buf.factor_version[s]
        out["without_value"][i] = int(np.isnan(buf.raw[s]).sum())
    return out


def _window_hours(kind: str, slots_hour: np.ndarray, x: int) -> np.ndarray:
    """Positions in the (oldest-first) slot arrays of the hours of window ``x``."""
    m = slots_hour.size
    return np.arange(m - x, m) if kind == "observed" else np.arange(x)


# --- totals -------------------------------------------------------------------------------------
@dataclass
class KindTotals:
    """Upstream totals of one kind (observed or forecast) for every window: arrays ``(windows, n)``."""

    kind: str
    windows: tuple[int, ...]
    mm: np.ndarray            # float32 upstream mean depth
    miss_hours: np.ndarray    # uint8 most hours missing from one catchment upstream
    miss_frac: np.ndarray     # float32 share of upstream area-hours without a value
    products: np.ndarray      # uint8 bits over product_tags
    scale: np.ndarray         # uint8 RAW | SCALED
    product_tags: tuple[str, ...]
    slots: dict[str, np.ndarray]   # per hour of the longest window, oldest first
    info: dict = field(default_factory=dict)   # source, buffer, anchor, per-window summary; zones (the buffer's)
    up_zones: np.ndarray | None = None   # uint32/64 bit per scaling zone (info["zones"]) upstream (rf-t26.16)

    def index(self, x: int) -> int:
        try:
            return self.windows.index(int(x))
        except ValueError:
            raise KeyError(f"{self.kind} window {x} h was not computed (have {list(self.windows)}); "
                           f"recompute with rain totals --windows") from None

    def product_names(self, bits: int) -> list[str]:
        return [t for k, t in enumerate(self.product_tags) if bits >> k & 1]


class UpstreamTotals:
    """Upstream rainfall totals of every reach: the network fields plus :class:`KindTotals` per kind."""

    STATIC = ("gid", "down", "vi", "region_of", "up_area_m2", "table_area_km2", "flags", "up_regions")
    PER_WINDOW = ("mm", "miss_hours", "miss_frac", "products", "scale")

    def __init__(self, static: dict[str, np.ndarray], regions: tuple[str, ...], kinds: dict[str, KindTotals],
                 meta: dict):
        for k in self.STATIC:
            setattr(self, k, static[k])
        self.regions, self.kinds, self.meta = tuple(regions), kinds, meta
        self._sorted = None

    @property
    def n(self) -> int:
        return int(self.gid.size)

    @property
    def windows(self) -> tuple[int, ...]:
        return tuple(self.meta["windows"])

    def kind(self, kind: str) -> KindTotals:
        if kind not in self.kinds:
            raise KeyError(f"no {kind} totals (have {', '.join(self.kinds) or 'none'})")
        return self.kinds[kind]

    # --- arrays over all reaches -----------------------------------------------------------------
    def mm(self, kind: str, x: int, strict: bool = False) -> np.ndarray:
        """Upstream mean depth (mm, float32); ``strict``: NaN where any hour is missing upstream."""
        k = self.kind(kind)
        i = k.index(x)
        return np.where(k.miss_hours[i] == 0, k.mm[i], np.nan).astype(np.float32) if strict else k.mm[i]

    def m3(self, kind: str, x: int, strict: bool = False) -> np.ndarray:
        """Upstream volume (m³, float64) = mm / 1000 x upstream pixel area."""
        return self.mm(kind, x, strict).astype(np.float64) / 1000.0 * self.up_area_m2

    def miss_hours(self, kind: str, x: int) -> np.ndarray:
        k = self.kind(kind)
        return k.miss_hours[k.index(x)]

    def miss_frac(self, kind: str, x: int) -> np.ndarray:
        k = self.kind(kind)
        return k.miss_frac[k.index(x)]

    # --- rows -----------------------------------------------------------------------------------
    def rows_of(self, gids) -> np.ndarray:
        """Row of each gid (-1 if unknown)."""
        if self._sorted is None:
            o = np.argsort(self.gid, kind="stable")
            self._sorted = (o, self.gid[o])
        o, g = self._sorted
        q = np.atleast_1d(np.asarray(gids, dtype=np.int64))
        if g.size == 0:
            return np.full(q.size, -1, dtype=np.int64)
        pos = np.clip(np.searchsorted(g, q), 0, g.size - 1)
        return np.where(g[pos] == q, o[pos], -1).astype(np.int64)

    def flag_names(self, bits: int) -> list[str]:
        return [name for b, name in FLAG_NAMES.items() if bits & b]

    def region_names(self, bits: int) -> list[str]:
        return [r for k, r in enumerate(self.regions) if int(bits) >> k & 1]

    def at(self, row: int) -> dict:
        """One reach: network fields and, per kind and window, mm, m³, missing hours and share, products, scale."""
        r = int(row)
        if not 0 <= r < self.n:
            raise IndexError(f"row {row} outside 0..{self.n - 1}")
        out = {"row": r, "gid": int(self.gid[r]), "region": self.regions[self.region_of[r]]
               if self.region_of[r] >= 0 else None, "vi": int(self.vi[r]),
               "up_area_km2": round(float(self.up_area_m2[r]) / 1e6, 4),
               "table_area_km2": round(float(self.table_area_km2[r]), 4),
               "flags": self.flag_names(int(self.flags[r])), "down": int(self.down[r])}
        for kind, k in self.kinds.items():
            out[kind] = {x: {"mm": float(k.mm[i, r]), "m3": float(k.mm[i, r]) / 1000.0 * float(self.up_area_m2[r]),
                             "miss_hours": int(k.miss_hours[i, r]), "miss_frac": float(k.miss_frac[i, r]),
                             "products": k.product_names(int(k.products[i, r])),
                             "scale": SCALE_NAMES[int(k.scale[i, r])]}
                         for i, x in enumerate(k.windows)}
        return out

    def frame(self, rows=None, kinds=None, windows=None,
              fields=("mm", "m3", "miss_hours", "miss_frac", "products", "scale")) -> pd.DataFrame:
        """A table of ``rows`` (default all), one row each: gid, region, vi, areas, flags, then per kind,
        field and window a column ``<obs|fc>_<field>_<x>h``."""
        rows = np.arange(self.n) if rows is None else np.asarray(rows, dtype=np.int64)
        reg = np.asarray(self.regions + ("",), dtype=object)
        df = {"gid": self.gid[rows], "region": reg[self.region_of[rows]], "vi": self.vi[rows],
              "up_area_km2": self.up_area_m2[rows] / 1e6, "table_area_km2": self.table_area_km2[rows],
              "flags": self.flags[rows]}
        for kind in kinds or self.kinds:
            k = self.kind(kind)
            p = PREFIX[kind]
            for x in windows or k.windows:
                i = k.index(x)
                for f in fields:
                    if f == "m3":
                        v = k.mm[i, rows].astype(np.float64) / 1000.0 * self.up_area_m2[rows]
                    else:
                        v = getattr(k, f)[i, rows]
                    df[f"{p}_{f}_{x}h"] = v
        return pd.DataFrame(df)

    def for_gids(self, gids, **kw) -> pd.DataFrame:
        """:meth:`frame` for the given gids, in that order; unknown gids raise ``KeyError``."""
        rows = self.rows_of(gids)
        if (rows < 0).any():
            bad = np.atleast_1d(np.asarray(gids))[rows < 0]
            raise KeyError(f"unknown gid(s): {', '.join(str(int(g)) for g in bad[:10])}")
        return self.frame(rows, **kw)

    def viewer_rows(self, region: str | None = None) -> dict[str, np.ndarray]:
        """Rows of the viewer reaches (``vi >= 0``) per region, sorted by ``vi`` (position j holds vi j)."""
        out = {}
        for k, name in enumerate(self.regions):
            if region is not None and name != region:
                continue
            rows = np.flatnonzero((self.region_of == k) & (self.vi >= 0))
            out[name] = rows[np.argsort(self.vi[rows], kind="stable")]
        return out

    def provenance(self, row: int, kind: str, x: int) -> list[dict]:
        """Every hour of window ``x`` for reach ``row``: product, source, run, lead and the factor
        version and factor of each scaling zone upstream of the reach (zones: the buffer's, rf-t26.16;
        totals saved before zones name the regions)."""
        k = self.kind(kind)
        k.index(x)
        s = k.slots
        if k.up_zones is not None and k.info.get("zones"):
            names, bits = tuple(k.info["zones"]), int(k.up_zones[row])
        else:
            names, bits = self.regions, int(self.up_regions[row])
        regs = [i for i in range(len(names)) if bits >> i & 1]
        out = []
        for j in _window_hours(kind, s["hour"], x):
            code = int(s["product"][j])
            out.append({"hour": _hour(s["hour"][j]), "product": BY_CODE[code].tag if code else None,
                        "source": str(s["source"][j]) or None,
                        "run": _hour(s["run_hour"][j]) if s["run_hour"][j] >= 0 else None,
                        "lead": int(s["lead"][j]) if s["lead"][j] >= 0 else None,
                        "factor_version": {names[i]: int(s["factor_version"][j, i]) for i in regs},
                        "factor": {names[i]: float(s["factor"][j, i]) for i in regs}})
        return out

    # --- files ----------------------------------------------------------------------------------
    def save(self, path: Path, compress: bool = True) -> Path:
        """``totals.npz`` and, next to it, ``totals.json`` (the metadata)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {k: getattr(self, k) for k in self.STATIC}
        arrays["down"] = arrays["down"].astype(np.int32)
        for kind, k in self.kinds.items():
            p = PREFIX[kind]
            for f in self.PER_WINDOW:
                arrays[f"{p}_{f}"] = getattr(k, f)
            for f, v in k.slots.items():
                arrays[f"{p}_slot_{f}"] = v
            if k.up_zones is not None:
                arrays[f"{p}_up_zones"] = k.up_zones
        meta = dict(self.meta, format=FORMAT, regions=list(self.regions),
                    kinds={kind: {"windows": list(k.windows), "product_tags": list(k.product_tags), **k.info}
                           for kind, k in self.kinds.items()})
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            (np.savez_compressed if compress else np.savez)(fh, meta=np.array(json.dumps(meta)), **arrays)
        os.replace(tmp, path)
        js = path.with_suffix(".json")
        tmpj = js.with_name(js.name + ".tmp")
        tmpj.write_text(json.dumps(meta, indent=1))
        os.replace(tmpj, js)
        self.meta = meta
        return path

    @classmethod
    def load(cls, path: Path) -> UpstreamTotals:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("format") != FORMAT:
                raise ValueError(f"{path}: not a {FORMAT} file")
            static = {k: z[k] for k in cls.STATIC}
            static["down"] = static["down"].astype(np.int64)
            kinds = {}
            for kind, km in meta["kinds"].items():
                p = PREFIX[kind]
                slots = {f[len(p) + 6:]: z[f] for f in z.files if f.startswith(f"{p}_slot_")}
                info = {k: v for k, v in km.items() if k not in ("windows", "product_tags")}
                kinds[kind] = KindTotals(kind, tuple(km["windows"]), *(z[f"{p}_{f}"] for f in cls.PER_WINDOW),
                                         tuple(km["product_tags"]), slots, info,
                                         z[f"{p}_up_zones"] if f"{p}_up_zones" in z.files else None)
        return cls(static, tuple(meta["regions"]), kinds, meta)


def _check_windows(windows) -> tuple[int, ...]:
    xs = sorted({int(x) for x in windows})
    if not xs or any(int(x) != x for x in windows) or xs[0] < 1 or xs[-1] > SLOTS:
        raise ValueError(f"windows must be whole hours in 1..{SLOTS}, not {list(windows)}")
    return tuple(xs)


def compute_totals(net: Network, buffers: dict[str, HourlyBuffer], windows=(1, 3, 6, 12, 24), scaled: bool = True,
                   anchor: dict[str, int] | None = None, now_hour: int | None = None) -> UpstreamTotals:
    """Upstream totals of every reach for every window of every buffer given (``{"observed": buf, ...}``).

    ``anchor`` per kind overrides where the windows sit (observed: their last hour, default the latest
    hour held, so hours after it count as missing; forecast: the hour before their first, default the
    buffer's "now"). ``now_hour`` only feeds the staleness fields of the metadata. Empty buffers are
    skipped. Any whole windows in 1..24 work: one pass over the hours serves them all.
    """
    t0 = time.perf_counter()
    xs = _check_windows(windows)
    n, W = net.n, len(xs)
    use = {}
    for kind, buf in buffers.items():
        if kind not in KINDS or buf.kind != kind:
            raise ValueError(f"buffer for {kind!r} is a {buf.kind} buffer")
        if buf.n != n or (buf.rows_digest and buf.rows_digest != net.rows_digest):
            raise ValueError(f"{kind} buffer rows do not match the network (different catchment table)")
        if buf.anchor >= 0:
            use[kind] = buf
    timing = {}
    local = {kind: _local_windows(buf, net.area_m2, xs, scaled, (anchor or {}).get(kind))
             for kind, buf in use.items()}
    timing["local_s"] = round(time.perf_counter() - t0, 3)
    t1 = time.perf_counter()
    K = len(local)
    # one row per quantity, kind j and window i (c = j * W + i): local volume and missing area-hours (sums),
    # missing hours (max), product and scale bits (or)
    s = np.empty((2 * W * K, n), dtype=np.float64)
    mx = np.empty((W * K, n), dtype=np.uint8)
    bits = np.empty((2 * W * K, n), dtype=np.uint8)
    for j, lo in enumerate(local.values()):
        c = slice(j * W, (j + 1) * W)
        c2 = slice(W * K + j * W, W * K + (j + 1) * W)
        s[c], mx[c], bits[c], bits[c2] = lo.m3, lo.n_missing, lo.products, lo.scale
        np.multiply(lo.n_missing, net.area_m2, out=s[c2])
    timing["pack_s"] = round(time.perf_counter() - t1, 3)
    t2 = time.perf_counter()
    order, down = net.order, net.down
    accumulate(order, down, s)
    accumulate(order, down, mx, "max")
    accumulate(order, down, bits, "or")
    timing["accumulate_s"] = round(time.perf_counter() - t2, 3)
    t3 = time.perf_counter()
    A = net.up_area_m2
    has_area = A > 0
    A1 = np.where(has_area, A, 1.0)
    kinds = {}
    is_down = down >= 0
    out_rows = ~is_down
    for j, (kind, lo) in enumerate(local.items()):
        buf = use[kind]
        c, c2 = slice(j * W, (j + 1) * W), slice(W * K + j * W, W * K + (j + 1) * W)
        mm = np.where(has_area, s[c] / A1 * 1000.0, lo.mm).astype(np.float32)
        mf = np.where(has_area, s[c2] / (np.asarray(xs, dtype=np.float64)[:, None] * A1),
                      (mx[c] > 0)).astype(np.float32)
        info_w = {}
        slots = _slots(buf, lo.hours)
        for i, x in enumerate(xs):
            m3 = s[j * W + i]
            # checks: every reach = its tributaries + its own local volume; the outlets hold all local volume
            trib = np.bincount(down[is_down], weights=m3[is_down], minlength=n)
            resid = np.abs(m3 - trib - lo.m3[i]) / np.maximum(np.abs(m3), 1.0)
            total_local, at_outlets = float(lo.m3[i].sum()), float(m3[out_rows].sum())
            info_w[str(x)] = _window_info(kind, x, slots, mx[j * W + i], mm[i], m3, net, {
                "mass_balance_max_rel_err": float(resid.max()) if n else 0.0,
                "outlets_m3": at_outlets, "local_m3": total_local,
                "outlets_vs_local_rel_err": abs(at_outlets - total_local) / max(total_local, 1.0)})
        up_zones = accumulate(order, down, _region_bits(buf.region_of, len(buf.regions)), "or")
        k = KindTotals(kind, xs, mm, mx[c], mf, bits[c], bits[c2], lo.tags, slots, {}, up_zones)
        k.info = _kind_info(kind, buf, int(np.bitwise_or.reduce(lo.scale, axis=None)) if lo.scale.size else 0,
                            now_hour, (anchor or {}).get(kind), info_w)
        k.info["zones"] = list(buf.regions)   # the scaling zones of the slot factors (rf-t26.16)
        kinds[kind] = k
    timing["finish_s"] = round(time.perf_counter() - t3, 3)
    timing["total_s"] = round(time.perf_counter() - t0, 3)
    static = {"gid": net.gid, "down": net.down, "vi": net.vi, "region_of": net.region_of,
              "up_area_m2": net.up_area_m2, "table_area_km2": net.table_area_km2, "flags": net.flags,
              "up_regions": net.up_regions}
    meta = {"created": datetime.now(UTC).isoformat(timespec="seconds"), "windows": list(xs),
            # scaled: some value summed used a gauge scale factor (version > 0); values: the array summed
            "scaled": any(k.info["scaled"] for k in kinds.values()), "values": "scaled" if scaled else "raw",
            "rows": n, "rows_digest": net.rows_digest, "down_digest": net.down_digest,
            "network": {k: net.meta.get(k) for k in ("built", "table", "weights", "flags", "area_check")},
            "flags_doc": FLAG_DOC, "seconds": timing,
            "missing_doc": {"miss_hours": "most hours missing from one local catchment upstream (0 = complete)",
                            "miss_frac": "share of upstream area x hours with no value"},
            "scale_doc": {"0": "no value", "1": "raw (factor version 0)", "2": "scaled (factor version > 0)",
                          "3": "mixed"}}
    return UpstreamTotals(static, net.regions, kinds, meta)


def _window_info(kind: str, x: int, slots: dict, miss: np.ndarray, mm: np.ndarray, m3: np.ndarray, net: Network,
                 checks: dict) -> dict:
    pos = _window_hours(kind, slots["hour"], x)
    hs = slots["hour"][pos]
    held = slots["product"][pos] > 0
    prods, sources, runs, versions = {}, {}, set(), set()
    for j in pos[held]:
        tag = BY_CODE[int(slots["product"][j])].tag
        prods[tag] = prods.get(tag, 0) + 1
        sources[str(slots["source"][j])] = sources.get(str(slots["source"][j]), 0) + 1
        if slots["run_hour"][j] >= 0:
            runs.add(int(slots["run_hour"][j]))
        versions.update(int(v) for v in slots["factor_version"][j])
    leads = slots["lead"][pos[held]]
    leads = leads[leads >= 0]
    imax = int(np.argmax(mm)) if mm.size else 0
    big = net.up_area_m2 >= 100e6
    ibig = int(np.flatnonzero(big)[np.argmax(mm[big])]) if big.any() else -1
    iv = int(np.argmax(m3)) if m3.size else 0
    first = int(hs[0]) - 1 if hs.size else None

    def where(i):
        return {"row": i, "gid": int(net.gid[i]), "region": net.regions[net.region_of[i]]
                if net.region_of[i] >= 0 else None, "up_area_km2": round(float(net.up_area_m2[i]) / 1e6, 3),
                "mm": round(float(mm[i]), 3), "m3": float(m3[i])}

    return {"start": _hour(first) if first is not None else None, "end": _hour(hs[-1]) if hs.size else None,
            "hours_with_data": int(held.sum()), "missing_hours": [_hour(h) for h in hs[~held]],
            "products": prods, "sources": sources, "runs": [_hour(r) for r in sorted(runs)],
            "leads": [int(leads.min()), int(leads.max())] if leads.size else None,
            "factor_versions": sorted(versions), "reaches_incomplete": int(np.count_nonzero(miss)),
            "max_mm": where(imax), "max_mm_ge_100km2": where(ibig) if ibig >= 0 else None, "max_m3": where(iv),
            "checks": checks}


def _kind_info(kind: str, buf: HourlyBuffer, scale: int, now_hour: int | None, anchor: int | None,
               windows: dict) -> dict:
    """``scale``: the scale bits of every local catchment and window OR-ed together (``scaled`` is true
    only when some value used a factor version > 0, as the ``SCALED`` bit of the reaches)."""
    held = buf.hour >= 0
    runs = sorted({int(r) for r in buf.run_hour[held] if r >= 0})
    info = {"anchor": _hour(buf.anchor), "scaled": bool(scale & SCALED), "scale": SCALE_NAMES[scale & (RAW | SCALED)],
            "windows_info": windows,
            "runs": [_hour(r) for r in runs], "sources": sorted({str(s) for s in buf.source[held]})}
    a = buf.anchor if anchor is None else int(anchor)
    if kind == "observed":
        info["window_end"] = _hour(a)
        if now_hour is not None:
            info["hours_behind_now"] = int(now_hour) - a
    else:
        s = a
        info["start"] = _hour(s)
        if runs:
            info["run"] = _hour(runs[-1])
            info["run_age_hours_at_start"] = s - runs[-1]
        if now_hour is not None:
            info["hours_since_start"] = int(now_hour) - s
    return info

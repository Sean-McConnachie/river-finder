"""Gauge scale factors (rf-t26.5) and copies of one station (rf-t26.13), without network: factor maths and
guard rails, versioning and history, the cell store and its ECMWF backfill rule, applying factors to buffers
(rain ecmwf, rain scale), the publish gate and the fallback without a factor file."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import scipy.sparse as sp

from river_finder.rainfall import CatchmentWeights, GridSpec, HourlyBuffer, Network, hour_index, hour_time, live
from river_finder.rainfall import pipeline as pl
from river_finder.rainfall import scaling as S
from river_finder.rainfall.buffer import ScaleFactors
from river_finder.rainfall.config import ScalingConfig, parse_config
from river_finder.rainfall.ecmwf import Reader, floor_run
from river_finder.rainfall.fetch import Bucket, Http
from river_finder.rainfall.gauges import MISSING, RAW, GaugeHours

GRID = GridSpec("ecmwf", lon0=170.0, lat0=-40.0, dlon=0.1, dlat=-0.1, nx=4, ny=3)   # lines up with ECMWF 0.1°
NOW = datetime(2026, 11, 22, 3, tzinfo=UTC)
NOW_H = hour_index(NOW)
REGIONS = ("Auckland", "Canterbury", "Tasman", "West Coast")
SC = ScalingConfig()


def _cfg(root, **scaling):
    return parse_config({"observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
                         "grids": {"ecmwf": {k: getattr(GRID, k) for k in ("lon0", "lat0", "dlon", "dlat", "nx",
                                                                           "ny")}},
                         "paths": {"nz_data": str(root)}, "scaling": scaling,
                         "gauges": {"councils": {
                             "wcrc": {"name": "West Coast Regional Council", "base_url": "https://w/",
                                      "hts": ["data.hts"], "region": "West Coast", "attribution": "Rain: WCRC",
                                      "licence": "not stated"},
                             "tdc": {"name": "Tasman District Council", "base_url": "https://t/", "hts": ["data.hts"],
                                     "region": "Tasman", "attribution": "Rain: TDC", "licence": "not stated"},
                             "ecan": {"name": "Environment Canterbury", "kind": "ecan-portal",
                                      "base_url": "https://e/", "region": "Canterbury", "attribution": "Rain: ECan",
                                      "licence": "CC BY 4.0"}}}})


# --- synthetic gauges -------------------------------------------------------------------------------
def _gauges(spec, hours=900, end=NOW_H, rain=None):
    """``spec``: (id, region, cell, x, y, ratio[, usable share]) per gauge; gauge mm = ratio x ``rain(h)``."""
    rain = rain or (lambda h: 1.0)
    hour = np.arange(end - hours + 1, end + 1, dtype=np.int64)
    G = len(spec)
    mm = np.zeros((G, hours), np.float32)
    state = np.full((G, hours), RAW, np.uint8)
    regions = tuple(sorted({s[1] for s in spec}))
    for i, s in enumerate(spec):
        mm[i] = s[5] * np.array([rain(int(h)) for h in hour], np.float32)
        if len(s) > 6:  # the last part of the record missing
            n_missing = int(round((1 - s[6]) * hours))
            mm[i, hours - n_missing:] = np.nan
            state[i, hours - n_missing:] = MISSING
    cols = {"id": np.array([s[0] for s in spec]), "council": np.array([s[0].split(":")[0] for s in spec]),
            "name": np.array([s[0].split(":")[1] for s in spec]), "lon": np.full(G, 171.0), "lat": np.full(G, -42.0),
            "x": np.array([s[3] for s in spec], float), "y": np.array([s[4] for s in spec], float),
            "cell": np.array([s[2] for s in spec], np.int32), "cell_row": np.array([s[2] // 4 for s in spec], np.int32),
            "cell_col": np.array([s[2] % 4 for s in spec], np.int32),
            "region_of": np.array([regions.index(s[1]) for s in spec], np.int16)}
    meta = {"created": "2026-11-22T03:10:00Z", "grid": dict(GRID.to_dict(), cell="row * nx + col"),
            "councils": {c: {"name": n, "attribution": f"Rain: {c.upper()}", "licence": lic, "base_url": f"https://{c}/"}
                         for c, n, lic in (("wcrc", "West Coast Regional Council", "not stated"),
                                           ("tdc", "Tasman District Council", "not stated"),
                                           ("ecan", "Environment Canterbury", "CC BY 4.0"))}}
    return GaugeHours(hour, mm, np.full((G, hours), -1, np.int16), state, np.zeros((G, hours), np.uint8), cols,
                      regions, meta)


SPEC = [  # West Coast: 4 gauges (ratio of sums 1.6), a TDC copy of the first, one with half its hours
    ("wcrc:A", "West Coast", 0, 1.50e6, 5.30e6, 1.4), ("tdc:A copy", "West Coast", 0, 1.50e6, 5.3005e6, 1.4),
    ("wcrc:B", "West Coast", 1, 1.51e6, 5.30e6, 1.6), ("wcrc:C", "West Coast", 4, 1.52e6, 5.30e6, 1.8),
    ("wcrc:D", "West Coast", 5, 1.53e6, 5.30e6, 1.6), ("wcrc:E", "West Coast", 5, 1.54e6, 5.30e6, 3.0, 0.5),
    ("tdc:F", "Tasman", 2, 1.60e6, 5.40e6, 1.2), ("tdc:G", "Tasman", 3, 1.61e6, 5.40e6, 1.2),       # too few
    ("ecan:H", "Canterbury", 8, 1.70e6, 5.20e6, 0.9), ("ecan:I", "Canterbury", 9, 1.71e6, 5.20e6, 0.9),
    ("ecan:J", "Canterbury", 10, 1.72e6, 5.20e6, 0.9),
]


def _wet(h):  # rain every third hour, varying: copies need wet hours to be told apart from neighbours
    return float((h % 3 == 0) * (1 + (h % 7)))


# --- readers --------------------------------------------------------------------------------------
class FakeReader(Reader):
    """Runs every 6 h with leads 1..18 from ``first_run`` up to the run whose lead 5 has ended at ``now``;
    value = ``base`` mm at every cell (``base`` may depend on the lead); counts every field read."""

    def __init__(self, name, first_run, now, base=lambda run, lead: 1.0):
        self.name = name
        super().__init__(GRID)
        self.first_run, self.now, self.base, self.reads = first_run, now, base, []
        self.bucket = Bucket(["https://nowhere/"], Http(retries=0))

    def _list_leads(self, run):
        if run < self.first_run or run + timedelta(hours=5) > self.now:
            return ()
        return tuple(range(1, 91))

    def hourly(self, run, lead):
        assert lead in self.leads(run)
        self.reads.append((run, lead))
        return np.full(GRID.shape, self.base(run, lead), np.float32), "fake"


def _readers(now, recent_days=8):
    """Open-Meteo-like (recent runs only) first, ECMWF-like (everything) second."""
    return [FakeReader("openmeteo-s3", floor_run(now - timedelta(days=recent_days)), now),
            FakeReader("ecmwf-s3", floor_run(now - timedelta(days=60)), now)]


# --- factor maths and guard rails --------------------------------------------------------------------
def _ev(n=5, gauge=500.0, grid=400.0):
    return {"gauges": n, "mean_gauge_mm": gauge / max(n, 1), "mean_grid_mm": grid / max(n, 1),
            "ratio": gauge / grid if grid else None}


UNITY = {"factor": 1.0, "version": 0, "set": None, "max_version": 0}


def test_decide_guard_rails_and_versions():
    t = NOW
    d = S.decide(UNITY, _ev(gauge=600, grid=400), SC, t)                        # ratio 1.5: one step x1.25
    assert d["changed"] and (d["factor"], d["version"], d["status"]) == (1.25, 1, "scaled")
    assert d["target"] == 1.5 and d["guards"] == ["max_step"]
    d = S.decide(UNITY, _ev(gauge=1500, grid=400), SC, t)                       # 3.75: clamped to 2, step 1.25
    assert d["target"] == 2.0 and d["factor"] == 1.25 and d["guards"] == ["clamp", "max_step"]
    d = S.decide(UNITY, _ev(gauge=200, grid=800), SC, t)                        # 0.25: clamp 0.5, step 0.8
    assert (d["target"], d["factor"]) == (0.5, 0.8)
    d = S.decide(UNITY, _ev(gauge=404, grid=400), SC, t)                        # 1 %: below min_change
    assert not d["changed"] and (d["factor"], d["version"], d["status"]) == (1.0, 0, "unity")
    assert d["guards"] == ["min_change"]
    d = S.decide(UNITY, _ev(n=2), SC, t)                                        # too few gauges
    assert not d["changed"] and d["status"] == "unity" and d["guards"] == ["min_gauges"]
    d = S.decide(UNITY, _ev(n=5, gauge=100, grid=90), SC, t)                    # 20 mm per gauge: too dry
    assert not d["changed"] and d["guards"] == ["min_rain"] and "too dry" in d["reason"]

    cur = {"factor": 1.25, "version": 3, "set": "2026-11-21T20:00:00Z", "max_version": 3}
    d = S.decide(cur, _ev(gauge=600, grid=400), SC, t)                          # 7 h after version 3
    assert not d["changed"] and d["guards"] == ["min_update_hours"] and d["version"] == 3
    d = S.decide(cur, _ev(gauge=600, grid=400), SC, t, force=True)              # --force
    assert d["changed"] and (d["factor"], d["version"], d["status"]) == (1.5, 4, "updated")
    d = S.decide(cur, _ev(gauge=600, grid=400), SC, t + timedelta(hours=14))
    assert d["changed"] and d["factor"] == 1.5 and d["version"] == 4
    d = S.decide(cur, _ev(n=5, gauge=100, grid=90), SC, t + timedelta(days=2))  # dry: the factor is held
    assert not d["changed"] and (d["factor"], d["version"], d["status"]) == (1.25, 3, "held")
    d = S.decide(cur, _ev(n=1), SC, t)                                          # too few: back to 1, version 0
    assert d["changed"] and (d["factor"], d["version"], d["status"]) == (1.0, 0, "reset")
    d = S.decide({"factor": 1.0, "version": 0, "set": "2026-11-01T00:00:00Z", "max_version": 3},
                 _ev(gauge=480, grid=400), SC, t)                               # after a reset: max_version + 1
    assert (d["factor"], d["version"]) == (1.2, 4)
    d = S.decide(cur, _ev(gauge=600, grid=400), SC, t, grid_ok=False)          # grid side incomplete
    assert not d["changed"] and d["guards"] == ["grid_coverage"]


def test_evaluate_region_sums_usable_hours_and_exclusions():
    g = _gauges(SPEC, hours=720)
    cols = np.arange(720)
    grid = np.ones((720, g.n), np.float32)
    grid[:10, 3] = np.nan                                                       # wcrc:C: 10 hours without grid
    keep = np.ones(g.n, bool)
    keep[1] = False                                                             # the TDC copy
    ev = S.evaluate_region(g.region_rows()["West Coast"], g, cols, grid, keep, SC)
    assert ev["gauges_listed"] == 6 and ev["gauges"] == 4
    assert ev["excluded"]["copy"] == ["tdc:A copy"] and ev["excluded"]["few_hours"][0]["id"] == "wcrc:E"
    used = {u["id"]: u for u in ev["used"]}
    assert used["wcrc:C"]["hours"] == 710 and used["wcrc:C"]["grid_mm"] == 710
    assert ev["gauge_mm"] == pytest.approx(720 * (1.4 + 1.6 + 1.6) + 710 * 1.8, abs=0.5)
    assert ev["ratio"] == pytest.approx(ev["gauge_mm"] / ev["grid_mm"], abs=1e-4) == pytest.approx(1.6, abs=2e-3)
    assert ev["check"]["median_ratio"] == pytest.approx(1.6) and ev["check"]["q25"] == pytest.approx(1.55)
    assert ev["by_council"]["wcrc"]["gauges"] == 4 and "trimmed_ratio" not in ev["check"]   # < 10 gauges


# --- copies of one station -------------------------------------------------------------------------
def test_find_copies_keeps_one_and_says_why():
    hours = 300
    spec = [("wcrc:Karamea", "West Coast", 0, 1.50e6, 5.30e6, 1.0),             # copy pair, 15 m apart
            ("tdc:HY Karamea", "West Coast", 0, 1.50e6, 5.300015e6, 1.0),
            ("wcrc:Aorere", "Tasman", 1, 1.60e6, 5.40e6, 1.0),                    # copy pair, 2.4 km apart
            ("tdc:HY Aorere", "Tasman", 1, 1.6024e6, 5.40e6, 1.0),
            ("ecan:EWS", "Canterbury", 8, 1.70e6, 5.20e6, 1.0),                   # copies, ECan has fewer hours
            ("wcrc:EWS", "Canterbury", 8, 1.70e6, 5.2000005e6, 1.0),
            ("ecan:Own", "Canterbury", 8, 1.70e6, 5.200014e6, 1.0),               # co-located, 1 h ahead
            ("wcrc:Far", "West Coast", 4, 1.55e6, 5.30e6, 1.0)]                   # 50 km away
    g = _gauges(spec, hours=hours, rain=_wet)
    g.mm[4, -5:] = np.nan                                                       # ecan:EWS misses 5 hours
    g.state[4, -5:] = MISSING
    g.mm[6, :-1] = g.mm[6, 1:].copy()                                           # ecan:Own: each hour an hour early
    g.mm[6] *= 1.03
    g.mm[2, :] += np.where(np.arange(hours) == 5, 0.04, 0.0).astype(np.float32)  # within 0.05 mm: still a copy
    cfg = _cfg("/nonexistent")
    cp = S.find_copies(g, np.arange(hours), cfg.scaling, S._home_regions(cfg))
    assert cp["keep"].tolist() == [True, False, False, True, False, True, True, True]
    groups = {grp["kept"]: grp for grp in cp["groups"]}
    assert set(groups) == {"wcrc:Karamea", "tdc:HY Aorere", "wcrc:EWS"}
    assert "wcrc is the home council of West Coast" in groups["wcrc:Karamea"]["why"]["tdc:HY Karamea"]
    assert "tdc is the home council of Tasman" in groups["tdc:HY Aorere"]["why"]["wcrc:Aorere"]
    assert groups["wcrc:EWS"]["why"]["ecan:EWS"] == "more usable hours in the window (300 vs 295)"
    assert groups["tdc:HY Aorere"]["pairs"][0]["distance_m"] == pytest.approx(2400, abs=1)
    assert groups["wcrc:Karamea"]["pairs"][0]["equal_share"] == 1.0
    co = cp["colocated"]                                                        # only between kept gauges
    assert len(co) == 1 and {co[0]["a"], co[0]["b"]} == {"wcrc:EWS", "ecan:Own"}
    assert co[0]["best_lag_h"] == -1 and co[0]["note"].startswith("ecan:Own runs 1 h ahead of wcrc:EWS")
    # dry hours only: nothing to compare, nothing merged
    dry = _gauges(spec, hours=hours, rain=lambda h: 0.0)
    assert S.find_copies(dry, np.arange(hours), cfg.scaling)["keep"].all()
    # copies are found over every hour held, even when the window itself is dry
    g2 = _gauges(spec, hours=hours, rain=lambda h: _wet(h) if h < NOW_H - 100 else 0.0)
    cp2 = S.find_copies(g2, np.arange(hours - 100, hours), cfg.scaling)       # (ecan:Own unshifted: a copy too)
    assert (~cp2["keep"]).sum() == 4 and cp2["compared"]["hours"] == hours
    cp3 = S.find_copies(g2, np.arange(hours - 100, hours), cfg.scaling, detect_cols=np.arange(hours - 100, hours))
    assert cp3["keep"].all()


# --- the cell store and its backfill ----------------------------------------------------------------
def test_cell_store_grow_trim_values_roundtrip(tmp_path):
    s = S.CellStore.empty("ecmwf", GRID)
    s.ensure([5, 2], 100, 109)
    s.mm[:, :] = np.arange(10)[:, None]
    s.src[:, :] = s.reader_code("a")
    s.ensure([7, 2], 95, 104)                                                   # earlier hours, a new cell
    assert s.cells.tolist() == [2, 5, 7] and (s.first, s.last) == (95, 109)
    v = s.values([5, 7, 9], 98, 101)
    assert np.isnan(v[:2]).all() and v[2:, 0].tolist() == [0, 1] and np.isnan(v[:, 1:]).all()
    assert s.coverage([2, 5], 100, 109) == 1.0 and s.coverage([2, 7], 100, 109) == 0.0
    assert s.trim(103) == 8 and s.first == 103
    p = s.save(tmp_path / "c.npz")
    t = S.CellStore.load(p)
    assert t.cells.tolist() == [2, 5, 7] and t.first == 103 and t.readers == ["a"]
    assert np.array_equal(t.values([2], 103, 109), s.values([2], 103, 109))
    with pytest.raises(ValueError):
        s.ensure([12], 100, 101)                                                # outside the 4 x 3 grid


def test_fill_ecmwf_follows_the_observed_proxy_rule():
    now = NOW
    rd = _readers(now, recent_days=3)
    s = S.CellStore.empty("ecmwf", GRID)
    first = NOW_H - 10 * 24 + 1
    st = S.fill_ecmwf(s, rd, [0, 5], first, NOW_H, (1, 18), log=lambda m: None)
    assert st["written"] == 240 and not st["no_run"]
    latest = floor_run(now - timedelta(hours=5))                                # newest run with lead 5 out
    for h in range(first, NOW_H + 1):
        t = h - s.first
        run = hour_index(floor_run(hour_time(h - 1)))
        lead = h - run
        if run > hour_index(latest):                                            # after the newest run: its leads
            run, lead = hour_index(latest), h - hour_index(latest)
        assert (s.run_hour[t, 0], s.lead[t, 0]) == (run, lead)
        want = "openmeteo-s3" if run >= hour_index(floor_run(now - timedelta(days=3))) else "ecmwf-s3"
        assert s.readers[s.src[t, 0] - 1] == want
    assert 1 <= s.lead[:, 0].min() and s.lead[:-6, 0].max() <= 6                 # settled hours: leads 1-6
    # again at the same time: nothing new; 6 h later the newest hours of long leads give way to the next run
    n0 = len(rd[0].reads) + len(rd[1].reads)
    st = S.fill_ecmwf(s, rd, [0, 5], first, NOW_H, (1, 18), log=lambda m: None)
    assert st["written"] == st["replaced"] == 0 and len(rd[0].reads) + len(rd[1].reads) == n0
    assert s.lead[-3:, 0].tolist() == [7, 8, 9]                                  # 01-03Z from the 18Z run
    later = now + timedelta(hours=6)                                            # the 00Z run is out
    rd2 = _readers(later, recent_days=3)
    st = S.fill_ecmwf(s, rd2, [0, 5], first, NOW_H + 6, (1, 18), log=lambda m: None)
    assert st["written"] == 6 and st["replaced"] == 3                           # 04-09Z new, 01-03Z replaced
    assert s.lead[-9:, 0].tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert s.lead[:-3, 0].max() <= 6 and s.readers[s.src[-1, 0] - 1] == "openmeteo-s3"
    # a new cell for old hours is filled from the reader that still has them
    st = S.fill_ecmwf(s, rd2, [0, 5, 9], first, NOW_H + 6, (1, 18), log=lambda m: None)
    assert st["written"] == 246 and np.isfinite(s.values([9], first, NOW_H + 6)).all()


# --- the whole step: factors, history, versions, buffers ----------------------------------------------
def _data(root, spec=SPEC, rain=None, hours=900):
    """A data directory with weights (regions), gauges_hourly and observed / forecast buffers."""
    n = 4                                                                       # one catchment per region
    counts = sp.csr_matrix((np.ones(n, np.int32), (np.arange(n), [11, 8, 2, 0])), shape=(n, GRID.size))
    w = CatchmentWeights(GRID, counts, np.arange(n, dtype=np.int64) + 7, np.ones(n, np.int64), 1e6,
                         np.full(n, -1, np.int64), np.arange(n, dtype=np.int16), REGIONS,
                         np.ones(n, np.float32), np.ones(n, np.float32), {"built": "x"})
    rain_dir = root / "rain"
    w.save(rain_dir / "weights_ecmwf.npz")
    _gauges(spec, hours=hours, rain=rain).save(rain_dir / "gauges" / "gauges_hourly.npz")
    obs = HourlyBuffer.for_weights(w, "observed")
    for h in range(NOW_H - 23, NOW_H + 1):
        obs.put(h, np.full(n, 2.0, np.float32), "ECMWF_OBS", run_hour=h - 3, lead=3)
    obs.save(rain_dir / "obs_ecmwf.npz")
    fc = HourlyBuffer.for_weights(w, "forecast")
    fc.advance(NOW_H)
    for h in range(NOW_H + 1, NOW_H + 25):
        fc.put(h, np.full(n, 0.5, np.float32), "ECMWF_FC", run_hour=NOW_H - 5, lead=h - NOW_H + 5)
    fc.save(rain_dir / "fc_ecmwf.npz")
    net = Network.build(w.gid, np.full(n, -1), np.full(n, 1e6), np.arange(n, dtype=np.int16), REGIONS,
                        np.ones(n, np.float32), np.zeros(n, np.int32))
    net.save(rain_dir / "network.npz")
    return w


def _scale(cfg, now, **kw):
    return S.run_scale(cfg, now=now, readers=_readers(now), log=lambda m: None, **kw)


def test_run_scale_versions_history_and_buffers(tmp_path):
    cfg = _cfg(tmp_path)
    _data(tmp_path)
    doc = _scale(cfg, NOW)
    reg = doc["regions"]
    assert (reg["West Coast"]["factor"], reg["West Coast"]["version"], reg["West Coast"]["status"]) == (1.25, 1,
                                                                                                         "scaled")
    assert (reg["Canterbury"]["factor"], reg["Canterbury"]["version"]) == (0.9, 1)      # ratio 0.9: one step
    assert (reg["Tasman"]["factor"], reg["Tasman"]["version"], reg["Tasman"]["status"]) == (1.0, 0, "unity")
    assert (reg["Auckland"]["factor"], reg["Auckland"]["version"]) == (1.0, 0)
    ev = doc["last_update"]["regions"]["West Coast"]
    assert ev["ratio"] == pytest.approx(1.6, abs=1e-3) and ev["gauges"] == 4
    assert ev["decision"]["guards"] == ["max_step"] and ev["excluded"]["copy"] == ["tdc:A copy"]
    w = doc["last_update"]["window"]
    assert w["hours"] == 720 and w["to"] == "2026-11-22 03:00Z" and w["from"] == "2026-10-23 03:00Z"
    cp = doc["last_update"]["copies"]["groups"]
    assert cp[0]["kept"] == "wcrc:A" and cp[0]["dropped"] == ["tdc:A copy"]
    hist = S.load_history(cfg, "ecmwf")
    assert [(h["seq"], h["region"], h["version"]) for h in hist] == [(1, "Canterbury", 1), (2, "West Coast", 1)]
    rec = hist[1]
    assert reg["West Coast"]["record"] == 2 and rec["previous"] == {"factor": 1.0, "version": 0}
    assert sorted(u["id"] for u in rec["evaluation"]["used"]) == ["wcrc:A", "wcrc:B", "wcrc:C", "wcrc:D"]
    assert rec["window"] == w and rec["copies_dropped"] == ["tdc:A copy"] and rec["products"] == ["ECMWF_OBS",
                                                                                                      "ECMWF_FC"]
    assert rec["inputs"]["gauges"]["created"] == "2026-11-22T03:10:00Z"
    # the saved buffers were re-scaled: raw kept, scaled = raw x factor of the catchment's region
    for kind, raw in (("observed", 2.0), ("forecast", 0.5)):
        b = HourlyBuffer.load(cfg.buffer_path(kind, "ecmwf"))
        s = next(s for s in range(24) if b.hour[s] >= 0)
        assert b.raw[s].tolist() == [raw] * 4
        assert b.scaled[s].tolist() == pytest.approx([raw, raw * 0.9, raw, raw * 1.25])
        assert b.factor_version[s].tolist() == [0, 1, 0, 1]
    # an hour later: within min_update_hours, nothing changes and no record is written
    doc = _scale(cfg, NOW + timedelta(hours=1))
    assert doc["regions"]["West Coast"]["version"] == 1 and doc["regions"]["West Coast"]["status"] == "held"
    assert "min_update_hours" in doc["last_update"]["regions"]["West Coast"]["decision"]["guards"]
    assert len(S.load_history(cfg, "ecmwf")) == 2
    # one step a day towards the ratio, then it stays (changes below 2 %)
    f = [1.25]
    for day in (1, 2, 3):
        doc = _scale(cfg, NOW + timedelta(days=day))
        f.append(doc["regions"]["West Coast"]["factor"])
    assert f == [1.25, 1.5625, 1.6, 1.6] and doc["regions"]["West Coast"]["version"] == 3
    assert doc["regions"]["Canterbury"]["version"] == 1                         # 0.9 reached at once
    hist = S.load_history(cfg, "ecmwf")
    assert [h["version"] for h in hist if h["region"] == "West Coast"] == [1, 2, 3]
    assert [h["seq"] for h in hist] == [1, 2, 3, 4]
    assert S.factors_for(cfg, "ECMWF_FC", REGIONS).factor.tolist() == pytest.approx([1.0, 0.9, 1.0, 1.6])


def test_run_scale_holds_when_the_grid_is_missing_and_resets_without_gauges(tmp_path):
    cfg = _cfg(tmp_path)
    _data(tmp_path)
    # no grid values (no reader has the runs): nothing changes
    doc = S.run_scale(cfg, now=NOW, readers=[FakeReader("ecmwf-s3", NOW + timedelta(days=9), NOW)],
                      log=lambda m: None)
    wc = doc["regions"]["West Coast"]
    assert (wc["factor"], wc["version"]) == (1.0, 0) and doc["last_update"]["inputs"]["cells"]["window_coverage"] == 0
    assert doc["last_update"]["regions"]["West Coast"]["decision"]["guards"] == ["grid_coverage"]
    assert not S.load_history(cfg, "ecmwf")
    _scale(cfg, NOW)
    # --no-fetch with the cells that are there; the gauges of the West Coast go missing: factor 1, version 0
    spec = [s if s[1] != "West Coast" else s[:6] + (0.1,) for s in SPEC]
    _data(tmp_path, spec=spec)
    doc = S.run_scale(cfg, now=NOW + timedelta(days=2), fetch=False, log=lambda m: None)
    wc = doc["regions"]["West Coast"]
    assert (wc["factor"], wc["version"], wc["status"]) == (1.0, 0, "reset") and wc["max_version"] == 1
    assert S.load_history(cfg, "ecmwf")[-1]["status"] == "reset"
    assert S.factors_for(cfg, "ECMWF_OBS", REGIONS).version.tolist() == [0, 1, 0, 0]


# --- applying: rain ecmwf, no factor file, apply off, products ------------------------------------------
def _factor_file(cfg, wc=1.5, version=2):
    S._write_json(cfg.factors_path("ecmwf"), {
        "format": S.FACTORS_FORMAT, "family": "ecmwf", "products": ["ECMWF_OBS", "ECMWF_FC"],
        "updated": "2026-11-22T03:00:00Z",
        "regions": {"West Coast": {"factor": wc, "version": version, "set": "2026-11-22T03:00:00Z", "status": "scaled"},
                    "Tasman": {"factor": 1.0, "version": 0, "set": None, "status": "unity"}},
        "councils": {"wcrc": {"name": "West Coast Regional Council", "attribution": "Rain: WCRC",
                              "licence": "not stated", "base_url": "https://w/"}},
        "last_update": {"window": {"from": "a", "to": "b"},
                        "regions": {"West Coast": {"used": [{"id": "wcrc:A", "council": "wcrc"}]}}}})


def test_factors_for_fallbacks(tmp_path):
    cfg = _cfg(tmp_path)
    f = S.factors_for(cfg, "ECMWF_OBS", REGIONS)                                # no factor file: unity
    assert f.factor.tolist() == [1, 1, 1, 1] and f.version.tolist() == [0, 0, 0, 0]
    _factor_file(cfg)
    assert S.factors_for(cfg, "ECMWF_OBS", REGIONS).version.tolist() == [0, 0, 0, 2]
    assert S.factors_for(cfg, "ECMWF_FC", REGIONS).factor.tolist() == [1, 1, 1, 1.5]
    off = _cfg(tmp_path, apply=False)
    assert S.factors_for(off, "ECMWF_OBS", REGIONS).version.tolist() == [0, 0, 0, 0]
    obs_only = _cfg(tmp_path, ecmwf={"products": ["ECMWF_OBS"]})
    assert S.factors_for(obs_only, "ECMWF_FC", REGIONS).version.tolist() == [0, 0, 0, 0]
    assert S.factors_for(obs_only, "ECMWF_OBS", REGIONS).version.tolist() == [0, 0, 0, 2]
    with pytest.raises(ValueError, match="not a ecmwf product"):
        _cfg(tmp_path, ecmwf={"products": ["GSMAP_NOW"]})
    with pytest.raises(ValueError, match="must contain 1"):
        _cfg(tmp_path, clamp=[1.1, 2.0])
    with pytest.raises(ValueError, match="unknown key"):
        _cfg(tmp_path, shrink=True)


def test_rain_ecmwf_stores_raw_scaled_factor_and_version(tmp_path):
    cfg = _cfg(tmp_path)
    w = _data(tmp_path)
    _factor_file(cfg)
    for kind in ("observed", "forecast"):                                       # start from empty buffers
        cfg.buffer_path(kind, "ecmwf").unlink()
    rd = FakeReader("fake", floor_run(NOW - timedelta(days=3)), NOW, base=lambda run, lead: 1.0)
    rep = pl.run_ecmwf(cfg, reader=rd, now=NOW, log=lambda m: None)
    for kind in ("observed", "forecast"):
        b = HourlyBuffer.load(cfg.buffer_path(kind, "ecmwf"))
        held = b.hour >= 0
        assert held.sum() == 24
        assert np.allclose(b.raw[held], 1.0) and np.allclose(b.scaled[held], [1.0, 1.0, 1.0, 1.5])
        assert (b.factor_version[held] == [0, 0, 0, 2]).all() and np.allclose(b.factor[held], [1, 1, 1, 1.5])
        assert rep[kind]["scaled"] and rep[kind]["factor_versions"] == [0, 2]
        assert rep[kind]["scale"]["regions"] == {"West Coast": {"factor": 1.5, "version": 2}}
    assert w.n == 4
    # apply = false: the same run writes unscaled buffers (version 0), whatever the factor file says
    rep = pl.run_ecmwf(_cfg(tmp_path, apply=False), reader=rd, now=NOW, log=lambda m: None)
    b = HourlyBuffer.load(cfg.buffer_path("observed", "ecmwf"))
    assert not rep["observed"]["scaled"] and (b.factor_version == 0).all() and np.allclose(b.scaled[b.hour >= 0], 1)


def test_apply_factors_rescales_held_hours_only_of_the_family():
    regions = ("A", "B")
    b = HourlyBuffer(2, "observed", regions, np.array([0, 1]))
    b.put(10, np.array([1.0, 2.0]), "ECMWF_OBS", run_hour=7, lead=3)
    b.put(11, np.array([1.0, np.nan]), "ECMWF_OBS", run_hour=7, lead=4)
    f = ScaleFactors("ecmwf", regions, [1.0, 1.2], [0, 5])
    assert b.apply_factors(f) == 2
    assert b.scaled[10 % 24].tolist() == pytest.approx([1.0, 2.4]) and np.isnan(b.scaled[11 % 24, 1])
    assert b.raw[10 % 24].tolist() == [1.0, 2.0] and b.factor_version[10 % 24].tolist() == [0, 5]
    assert b.apply_factors(ScaleFactors.unity("gsmap", regions)) == 0           # another family: untouched


# --- the publish gate ------------------------------------------------------------------------------
def test_publish_gate_raw_unless_scaling_publish(tmp_path):
    cfg = _cfg(tmp_path)
    _data(tmp_path)
    _factor_file(cfg, wc=1.5, version=2)
    S.apply_to_buffers(cfg, "ecmwf", log=lambda m: None)
    tot = pl.run_totals(cfg, now=NOW, log=lambda m: None)
    assert tot.meta["scaled"] and tot.meta["values"] == "scaled"
    wc = REGIONS.index("West Coast")
    assert tot.mm("observed", 24)[wc] == pytest.approx(24 * 2.0 * 1.5)        # local totals: scaled
    # publish = false (default): the raw sums of the same buffers, nothing says scaled
    out = tmp_path / "pub"
    idx = live.publish(cfg, out, now=NOW + timedelta(minutes=20), log=lambda m: None)
    assert idx["observed"]["scaled"] is idx["forecast"]["scaled"] is idx["totals"]["scaled"] is False
    assert idx["observed"]["scale"] == "raw" and idx["totals"]["values"] == "raw"
    assert idx["scaling"]["withheld"] and not idx["scaling"]["publish"] and "factors" not in idx["scaling"]
    assert "rf-t26.14" in idx["scaling"]["note"] and "WCRC" not in idx["attribution"]
    head, f = live.decode_region(gzip.decompress((out / "west-coast.bin.gz").read_bytes()))
    assert f["obs_mm_24h"][0] == pytest.approx(48.0, abs=0.05) and f["obs_scale_24h"][0] == 1
    assert f["fc_mm_24h"][0] == pytest.approx(12.0, abs=0.05) and f["fc_scale_24h"][0] == 1
    # publish = true: the scaled sums, the factors and the gauge councils' licences
    idx = live.publish(_cfg(tmp_path, publish=True), out, now=NOW + timedelta(minutes=20), log=lambda m: None)
    assert idx["observed"]["scaled"] and idx["totals"]["values"] == "scaled" and idx["observed"]["scale"] == "mixed"
    assert idx["scaling"]["factors"]["ecmwf"]["regions"] == {"West Coast": {"factor": 1.5, "version": 2,
                                                                           "set": "2026-11-22T03:00:00Z"}}
    assert "Rain: WCRC" in idx["attribution"] and any("West Coast Regional Council" in x["name"]
                                                       for x in idx["licences"])
    head, f = live.decode_region((out / "west-coast.bin.gz").read_bytes())
    assert f["obs_mm_24h"][0] == pytest.approx(72.0, abs=0.05) and f["obs_scale_24h"][0] == 2
    head, f = live.decode_region((out / "tasman.bin.gz").read_bytes())
    assert f["obs_mm_24h"][0] == pytest.approx(48.0, abs=0.05) and f["obs_scale_24h"][0] == 1
    # buffers changed after the totals: the raw sums cannot stand in for them
    b = HourlyBuffer.load(cfg.buffer_path("observed", "ecmwf"))
    b.put(NOW_H + 1, np.full(4, 1.0, np.float32), "ECMWF_OBS", run_hour=NOW_H - 2, lead=3)
    b.save(cfg.buffer_path("observed", "ecmwf"))
    with pytest.raises(SystemExit, match="changed since"):
        live.publish(cfg, out, now=NOW, log=lambda m: None)


def test_no_factor_file_publishes_unscaled_without_recomputing(tmp_path):
    cfg = _cfg(tmp_path)
    _data(tmp_path)
    pl.run_totals(cfg, now=NOW, log=lambda m: None)
    (tmp_path / "rain" / "network.npz").unlink()                               # publish must not need it
    idx = live.publish(cfg, tmp_path / "pub", now=NOW, log=lambda m: None)
    assert not idx["scaling"]["withheld"] and not idx["scaling"]["scaled"] and idx["totals"]["values"] == "scaled"
    lines = S.status_lines(cfg)
    assert "no factors yet" in lines[1]


def test_status_lines_and_table(tmp_path):
    cfg = _cfg(tmp_path)
    _data(tmp_path)
    logs = []
    S.run_scale(cfg, now=NOW, readers=_readers(NOW), log=logs.append)
    text = "\n".join(logs)
    assert "West Coast" in text and "1 zones without gauges" in text and "kept wcrc:A" in text
    lines = S.status_lines(cfg)
    assert "West Coast 1.250 v1" in lines[1] and "Canterbury 0.900 v1" in lines[1] and "cells" in lines[2]
    assert json.loads(cfg.factors_path("ecmwf").read_text())["format"] == S.FACTORS_FORMAT


def test_http_retries_a_body_cut_short():
    import http.client

    class Flaky(Http):
        def __init__(self):
            super().__init__(retries=2, backoff=0.0, sleep=lambda s: None)
            self.n = 0

        def _once(self, url, headers):
            self.n += 1
            if self.n == 1:
                raise http.client.IncompleteRead(b"x" * 10, 90)   # S3 dropped the connection mid-body
            return b"y" * 100, {}

    h = Flaky()
    assert h.get("https://bucket/key") == b"y" * 100 and h.n == 2 and h.stats.retries == 1


# --- likely outages (rf-t26.16) ------------------------------------------------------------------------
def test_find_outages_needs_wet_neighbours_and_a_wet_grid():
    hours = 300
    spec = [("ecan:Down", "Canterbury", 8, 1.70e6, 5.20e6, 1.0),     # no record for 100 h: an outage
            ("ecan:N1", "Canterbury", 8, 1.71e6, 5.20e6, 1.0),       # its neighbours, 10-20 km away
            ("ecan:N2", "Canterbury", 9, 1.70e6, 5.21e6, 1.0),
            ("ecan:N3", "Canterbury", 9, 1.72e6, 5.20e6, 1.0),
            ("ecan:Far", "Canterbury", 10, 1.80e6, 5.20e6, 1.0),     # dry too, but no gauge within 25 km
            ("ecan:Dry", "Canterbury", 10, 1.69e6, 5.20e6, 1.0),     # dry while the grid is dry: not an outage
            ("ecan:Short", "Canterbury", 9, 1.71e6, 5.21e6, 1.0)]    # dry for 20 h only
    g = _gauges(spec, hours=hours, rain=_wet)
    g.mm[0, 100:200] = 0
    g.mm[4, 100:200] = 0
    g.mm[5, 230:290] = 0
    g.mm[6, 30:50] = 0
    grid = np.ones((hours, g.n), np.float32)
    grid[230:290, 5] = 0
    keep = np.ones(g.n, bool)
    o = S.find_outages(g, grid, keep, SC)
    assert [s["id"] for s in o["spells"]] == ["ecan:Down"]
    s = o["spells"][0]
    assert s["dry_hours"] >= 100 and s["neighbours"] == 5 and s["neighbour_median_mm"] >= 20
    assert s["grid_mm"] >= 100 and s["nearest"][0]["km"] == 10.0
    assert o["mask"][0, 100:200].all() and not o["mask"][1:].any() and o["mask"][0].sum() == s["dry_hours"]
    # the outage hours are left out like missing hours: ecan:Down keeps 2/3 of the window (< 80 %)
    ev = S.evaluate_region(np.arange(4), g, np.arange(hours), grid, keep, SC, o["mask"])
    out = ev["excluded"]["few_hours"]
    assert [x["id"] for x in out] == ["ecan:Down"] and out[0]["outage_hours"] == s["dry_hours"]
    ev = S.evaluate_region(np.arange(4), g, np.arange(hours), grid, keep, SC)                 # without the rule
    assert ev["gauges"] == 4
    # dropped copies confirm nothing: one wet neighbour left is too few; the rule can be switched off
    keep[[1, 2, 3, 6]] = False
    assert not S.find_outages(g, grid, keep, SC)["spells"]
    off = _cfg("/nonexistent", outage_min_hours=0).scaling
    assert not S.find_outages(g, grid, np.ones(g.n, bool), off)["mask"].any()


# --- under-read events (rf-t26.20) ----------------------------------------------------------------------
UR_EVENT = (150, 174)    # hours (columns) of a 24-h storm: 5 mm/h in the grid, 120 mm


def _storm(h: int) -> float:
    i = h - (NOW_H - 300 + 1)                       # column of hour h in a 300-hour cache
    return 5.0 if UR_EVENT[0] <= i < UR_EVENT[1] else (0.5 if 20 <= i < 30 else 0.0)


def _underread_case():
    """West of the divide: a half-dead gauge (it tips 0.5 mm 5 times in the storm, so it is never dry for 24 h while
    the others are wet), one that is fine (0.8 x) and three wet neighbours; east of it, 10-20 km away, a rain-shadow
    cluster that reads 0.1 x while ECMWF spreads 0.8 x the storm onto its cells."""
    spec = [("wcrc:Half", "West Coast", 0, 1.505e6, 5.305e6, 1.0), ("wcrc:Fine", "West Coast", 0, 1.495e6, 5.30e6, 0.8),
            ("wcrc:W1", "West Coast", 0, 1.50e6, 5.30e6, 1.0), ("wcrc:W2", "West Coast", 1, 1.51e6, 5.30e6, 1.2),
            ("wcrc:W3", "West Coast", 1, 1.50e6, 5.31e6, 1.1),
            ("ecan:Lee", "Canterbury", 2, 1.52e6, 5.30e6, 0.1), ("ecan:Lee2", "Canterbury", 2, 1.53e6, 5.30e6, 0.12),
            ("ecan:Lee3", "Canterbury", 2, 1.525e6, 5.31e6, 0.1)]
    g = _gauges(spec, hours=300, rain=_storm)
    a, b = UR_EVENT
    g.mm[0, a:b] = 0.0
    g.mm[0, [a, a + 6, a + 12, a + 18, b - 1]] = 0.5
    grid = np.array([[_storm(int(h)) for h in g.hour]], np.float32).T * np.where(np.arange(g.n) >= 5, 0.8, 1.0)
    return g, grid.astype(np.float32)


def test_find_underreads_catches_a_half_dead_gauge_not_the_lee_side_or_a_fine_one():
    g, grid = _underread_case()
    keep = np.ones(g.n, bool)
    assert not S.find_outages(g, grid, keep, SC)["spells"]           # never dry for 24 h: the dry-spell rule misses it
    u = S.find_underreads(g, grid, keep, SC, S.find_outages(g, grid, keep, SC)["mask"])
    assert [e["id"] for e in u["events"]] == ["wcrc:Half"]
    e = u["events"][0]
    a, b = UR_EVENT
    assert e["hours"] == b - a and e["gauge_mm"] == 2.5 and e["grid_mm"] == 120.0     # its wet hours only
    assert e["neighbours"] == 4 and e["neighbour_median_mm"] == pytest.approx(126.0) and e["of_grid"] < 0.025
    assert e["windows_h"] == [24, 48, 72] and e["nearest"][0]["id"] in ("wcrc:W1", "wcrc:W3")
    assert u["mask"][0, a:b].all() and u["mask"].sum() == b - a                       # only its storm hours
    assert u["rule"]["same_side"] and u["rule"]["windows"][0] == {"hours": 24, "mm": 50.0}
    # its storm hours are left out like missing hours: the gauge keeps its ratio (1.0) outside the storm
    cols = np.arange(g.hour.size)
    ev = S.evaluate_region(np.arange(5), g, cols, grid, keep, SC, None, u["mask"])
    half = next(x for x in ev["used"] if x["id"] == "wcrc:Half")
    assert half["underread_hours"] == b - a and half["ratio"] == pytest.approx(1.0)
    ev0 = S.evaluate_region(np.arange(5), g, cols, grid, keep, SC)
    assert next(x for x in ev0["used"] if x["id"] == "wcrc:Half")["ratio"] < 0.2
    # across the main divide: the lee-side gauges read 12 % of ECMWF and 10 % of the wet gauges 10-20 km west. With
    # neighbours on their own side only (the default) that is no wet event; counting the west side, it is one
    both = replace(SC, underread_same_side=False)
    lee = S.find_underreads(g, grid, keep, both)
    assert {x["id"] for x in lee["events"]} == {"wcrc:Half", "ecan:Lee"}
    # ... unless ECMWF is dry there too (both must be wet)
    dry = grid.copy()
    dry[:, 5:] *= 0.1
    assert {x["id"] for x in S.find_underreads(g, dry, keep, both)["events"]} == {"wcrc:Half"}
    # a dropped copy is no neighbour; the rule can be switched off
    assert not S.find_underreads(g, grid, np.array([True, True, False, False, False, True, True, True]), SC)["events"]
    off = _cfg("/nonexistent", underread_hours=[], underread_mm=[]).scaling
    assert not S.find_underreads(g, grid, keep, off)["mask"].any()
    # a higher threshold makes the storm too small to judge
    assert not S.find_underreads(g, grid, keep, replace(SC, underread_mm=(150.0, 200.0, 250.0)))["events"]


def test_underread_config_is_checked(tmp_path):
    sc = _cfg(tmp_path).scaling
    assert (sc.underread_hours, sc.underread_mm, sc.underread_fraction) == ((24, 48, 72), (50.0, 75.0, 100.0), 0.2)
    sc = _cfg(tmp_path, underread_hours=[12, 36], underread_mm=[30, 60.5], underread_same_side=False).scaling
    assert sc.underread_hours == (12, 36) and sc.underread_mm == (30.0, 60.5) and not sc.underread_same_side
    with pytest.raises(ValueError, match="same length"):
        _cfg(tmp_path, underread_hours=[24, 48], underread_mm=[50])
    with pytest.raises(ValueError, match="shortest first"):
        _cfg(tmp_path, underread_hours=[48, 24], underread_mm=[50, 75])
    with pytest.raises(ValueError, match="underread_fraction"):
        _cfg(tmp_path, underread_fraction=1.5)
    assert S.describe_underread(sc) == "12 h >= 30 mm, 36 h >= 60.5 mm"
    assert S.describe_underread(replace(sc, underread_hours=(), underread_mm=())) == "off"


def test_run_scale_leaves_out_underread_events_and_records_them(tmp_path):
    storm = (NOW_H - 300, NOW_H - 277)

    def rain(h):
        return 6.0 if storm[0] <= h <= storm[1] else _wet(h) * 0.05      # drizzle (< 1 mm/h) around the storm

    def run(cfg):
        _data(cfg.rain_dir.parent, rain=rain)
        p = cfg.gauges_dir / "gauges_hourly.npz"
        g = GaugeHours.load(p)
        d = int(np.flatnonzero(np.asarray(g.id) == "wcrc:D")[0])
        c = np.flatnonzero((g.hour >= storm[0]) & (g.hour <= storm[1]))
        g.mm[d, c] = np.where(np.arange(c.size) % 6 == 0, 0.5, 0.0)     # wcrc:D tips now and then in the storm
        g.save(p)
        now = NOW
        rd = [FakeReader("ecmwf-s3", floor_run(now - timedelta(days=60)), now,
                         base=lambda run, lead: rain(hour_index(run) + lead))]
        return S.run_scale(cfg, now=now, readers=rd, log=lambda m: None)

    doc = run(_cfg(tmp_path / "on"))
    u = doc["last_update"]["underreads"]
    assert [x["id"] for x in u["events"]] == ["wcrc:D"] and u["window_hours"] == 24      # the storm's hours
    assert u["window_hours_by_council"]["wcrc"] == 24 and u["rule"]["fraction"] == 0.2
    ev = doc["last_update"]["regions"]["West Coast"]
    used = {x["id"]: x for x in ev["used"]}
    assert used["wcrc:D"]["underread_hours"] == 24 and used["wcrc:D"]["ratio"] == pytest.approx(1.6, abs=1e-3)
    assert ev["ratio"] == pytest.approx(1.6, abs=1e-3)
    rec = next(h for h in S.load_history(_cfg(tmp_path / "on"), "ecmwf") if h["region"] == "West Coast")
    assert [x["id"] for x in rec["underreads"]] == ["wcrc:D"] and rec["config"]["underread_hours"] == [24, 48, 72]
    lines = S.status_lines(_cfg(tmp_path / "on"))
    assert any("under-read events: 1 at 1 gauges, 24 gauge-hours" in x for x in lines)
    # without the rule, the storm that wcrc:D missed pulls the West Coast ratio down
    off = run(_cfg(tmp_path / "off", underread_hours=[], underread_mm=[]))
    assert not off["last_update"]["underreads"]["events"]
    assert off["last_update"]["regions"]["West Coast"]["ratio"] < 1.55


# --- scaling zones (rf-t26.16) -----------------------------------------------------------------------
ZREG = ("Auckland", "Canterbury", "Tasman", "West Coast")
ZXY = np.array([[1.9e6, 5.9e6], [1.695e6, 5.2e6], [1.75e6, 5.2e6], [1.6e6, 5.4e6], [1.5e6, 5.3e6]])  # per row
SPLIT = {"region": "Canterbury", "divide_km": 15.0, "near": "Canterbury near", "far": "Canterbury far"}
DIVIDE = {"west": ["West Coast"], "east": ["Canterbury"]}
SPEC_Z = [s for s in SPEC if s[1] != "Canterbury"] + [  # Canterbury: 1.2 x the grid near the divide, 0.5 x beyond
    ("ecan:K", "Canterbury", 8, 1.695e6, 5.20e6, 1.2), ("ecan:L", "Canterbury", 8, 1.700e6, 5.21e6, 1.2),
    ("ecan:M", "Canterbury", 8, 1.700e6, 5.19e6, 1.2), ("ecan:H", "Canterbury", 9, 1.72e6, 5.20e6, 0.5),
    ("ecan:I", "Canterbury", 10, 1.73e6, 5.20e6, 0.5), ("ecan:J", "Canterbury", 10, 1.74e6, 5.20e6, 0.5)]


def _line():
    import shapely

    return shapely.LineString([(1.69e6, 5.0e6), (1.69e6, 5.5e6)])   # the divide: x = 1690 km


def _zdata(root, spec=SPEC_Z):
    """As :func:`_data` with five catchments: Canterbury has one 5 km from the divide and one 60 km."""
    from river_finder.rainfall.weights import CatchmentWeights as CW

    n = 5
    region_of = np.array([0, 1, 1, 2, 3], np.int16)
    counts = sp.csr_matrix((np.ones(n, np.int32), (np.arange(n), [11, 8, 9, 2, 0])), shape=(n, GRID.size))
    w = CW(GRID, counts, np.arange(n, dtype=np.int64) + 7, np.ones(n, np.int64), 1e6, np.full(n, -1, np.int64),
           region_of, ZREG, np.ones(n, np.float32), np.ones(n, np.float32), {"built": "x", "pixel_area_m2": 1e6})
    rain_dir = root / "rain"
    w.save(rain_dir / "weights_ecmwf.npz")
    _gauges(spec, hours=900).save(rain_dir / "gauges" / "gauges_hourly.npz")
    obs = HourlyBuffer.for_weights(w, "observed")
    for h in range(NOW_H - 23, NOW_H + 1):
        obs.put(h, np.full(n, 2.0, np.float32), "ECMWF_OBS", run_hour=h - 3, lead=3)
    obs.save(rain_dir / "obs_ecmwf.npz")
    fc = HourlyBuffer.for_weights(w, "forecast")
    fc.advance(NOW_H)
    for h in range(NOW_H + 1, NOW_H + 25):
        fc.put(h, np.full(n, 0.5, np.float32), "ECMWF_FC", run_hour=NOW_H - 5, lead=h - NOW_H + 5)
    fc.save(rain_dir / "fc_ecmwf.npz")
    Network.build(w.gid, np.full(n, -1), np.full(n, 1e6), region_of, ZREG, np.ones(n, np.float32),
                  np.zeros(n, np.int32)).save(rain_dir / "network.npz")
    return w


def _zone_file(cfg, w):
    from river_finder.rainfall import zones as Z

    sc = cfg.scaling
    zm = Z.build_zones(Z.active_splits(cfg, w.regions), sc.divide_west, sc.divide_east, ZXY, w.regions, w.region_of,
                       w.rows_digest, _line(), w.npix)
    zm.save(cfg.zones_path)
    return zm


def test_zone_map_build_place_save_and_match(tmp_path):
    from river_finder.rainfall import zones as Z

    cfg = _cfg(tmp_path, zones=[SPLIT], divide=DIVIDE)
    w = _zdata(tmp_path)
    zm = _zone_file(cfg, w)
    assert zm.names == ("Auckland", "Canterbury near", "Canterbury far", "Tasman", "West Coast")
    assert zm.parent == ("Auckland", "Canterbury", "Canterbury", "Tasman", "West Coast")
    assert zm.zone_of.tolist() == [0, 1, 2, 3, 4] and not zm.identity
    assert zm.divide_km[1:3].tolist() == pytest.approx([5.0, 60.0]) and np.isnan(zm.divide_km[[0, 3, 4]]).all()
    assert zm.meta["zones_stats"]["Canterbury far"] == {"region": "Canterbury", "catchments": 1, "area_km2": 0.0}
    zi, km = zm.gauge_zones(["Canterbury", "Canterbury", "Canterbury", "West Coast", ""],
                            [1.704e6, 1.706e6, np.nan, 1.6e6, 1.6e6], [5.2e6] * 5)
    assert zi.tolist() == [1, 2, 2, 4, -1] and km[:2].tolist() == pytest.approx([14.0, 16.0]) and np.isnan(km[2:]).all()
    z2 = Z.ZoneMap.load(cfg.zones_path)
    assert z2.names == zm.names and np.array_equal(z2.zone_of, zm.zone_of) and z2.divide.length == 500_000
    assert z2.matches(cfg) and not z2.matches(_cfg(tmp_path, zones=[dict(SPLIT, divide_km=20.0)], divide=DIVIDE))
    assert Z.load_zones(cfg, log=lambda m: None).names == zm.names
    # no split of a region the network has: one zone per region, no file needed
    other = _cfg(tmp_path, zones=[dict(SPLIT, region="Otago", near="Otago near", far="Otago far")], divide=DIVIDE)
    assert Z.load_zones(other).identity and Z.load_zones(other).names == ZREG
    # a stale zone file: one zone per region and a note, unless factors would be applied with the wrong zones
    stale = _cfg(tmp_path, zones=[dict(SPLIT, divide_km=20.0)], divide=DIVIDE)
    notes = []
    assert Z.load_zones(stale, log=notes.append).identity and "other [[scaling.zones]]" in notes[0]
    _factor_file(stale)
    with pytest.raises(SystemExit, match="run river-finder rain zones"):
        Z.load_zones(stale)


def test_zone_config_is_checked(tmp_path):
    with pytest.raises(ValueError, match="missing far"):
        _cfg(tmp_path, zones=[{k: v for k, v in SPLIT.items() if k != "far"}])
    with pytest.raises(ValueError, match="one split per region"):
        _cfg(tmp_path, zones=[SPLIT, dict(SPLIT, near="a", far="b")])
    with pytest.raises(ValueError, match="zone names must be distinct"):
        _cfg(tmp_path, zones=[dict(SPLIT, far="Canterbury")])
    with pytest.raises(ValueError, match="unknown key"):
        _cfg(tmp_path, zones=[dict(SPLIT, km=3)])
    with pytest.raises(ValueError, match="distinct region lists"):
        _cfg(tmp_path, divide={"west": ["Canterbury"], "east": ["Canterbury"]})
    sc = _cfg(tmp_path, zones=[SPLIT]).scaling
    assert sc.split("Canterbury").near == "Canterbury near" and sc.split("Tasman") is None
    assert sc.divide_west == ("West Coast", "Tasman") and sc.outage_min_hours == 24


def test_rain_zones_from_region_polygons(tmp_path, monkeypatch):
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import box

    from river_finder.rainfall import zones as Z

    cfg = _cfg(tmp_path, zones=[SPLIT], divide=DIVIDE)
    w = _zdata(tmp_path)
    pd.DataFrame({"gid": w.gid, "x": ZXY[:, 0], "y": ZXY[:, 1],
                  "region": [ZREG[k] for k in w.region_of]}).to_pickle(cfg.segments_path)
    polys = gpd.GeoDataFrame({"region": ["West Coast", "Canterbury"]},
                             geometry=[box(1.60e6, 5.1e6, 1.69e6, 5.3e6), box(1.69e6, 5.1e6, 1.80e6, 5.3e6)],
                             crs="EPSG:2193")
    monkeypatch.setattr("river_finder.national.region_filters", lambda base=None: polys)
    zm = Z.run_zones(cfg, log=lambda m: None)
    assert zm.zone_of.tolist() == [0, 1, 2, 3, 4] and zm.meta["divide_length_km"] == pytest.approx(200.0)
    rep = json.loads(cfg.zones_path.with_suffix(".json").read_text())
    assert rep["zones"][1:3] == ["Canterbury near", "Canterbury far"] and rep["splits"][0]["divide_km"] == 15.0
    with pytest.raises(SystemExit, match="not a river-guide region"):
        Z.run_zones(_cfg(tmp_path, zones=[dict(SPLIT, region="Canterbery")], divide=DIVIDE), log=lambda m: None)


def test_zone_split_closes_the_region_and_starts_new_versions(tmp_path):
    plain = _cfg(tmp_path)
    zoned = _cfg(tmp_path, zones=[SPLIT], divide=DIVIDE)
    w = _zdata(tmp_path)
    # one Canterbury factor: (3 x 1.2 + 3 x 0.5) / 6 = 0.85
    doc = _scale(plain, NOW)
    assert (doc["regions"]["Canterbury"]["factor"], doc["regions"]["Canterbury"]["version"]) == (0.85, 1)
    # the split: without its zone file rain scale refuses (it would apply factors per the wrong zones)
    with pytest.raises(SystemExit, match="run river-finder rain zones"):
        _scale(zoned, NOW + timedelta(days=1))
    _zone_file(zoned, w)
    doc = _scale(zoned, NOW + timedelta(days=1))
    reg = doc["regions"]
    assert "Canterbury" not in reg and doc["closed"]["Canterbury"]["last_factor"] == 0.85
    assert (reg["Canterbury near"]["factor"], reg["Canterbury near"]["version"]) == (1.2, 1)
    assert (reg["Canterbury far"]["factor"], reg["Canterbury far"]["version"]) == (0.8, 1)     # 0.5: one step
    assert reg["Canterbury far"]["parent"] == "Canterbury" and reg["West Coast"]["version"] == 2   # its own steps
    ev = doc["last_update"]["regions"]
    assert sorted(u["id"] for u in ev["Canterbury near"]["used"]) == ["ecan:K", "ecan:L", "ecan:M"]
    assert ev["Canterbury far"]["ratio"] == pytest.approx(0.5) and ev["Canterbury far"]["parent"] == "Canterbury"
    assert doc["last_update"]["gauge_zones"]["ecan:K"] == {"zone": "Canterbury near", "divide_km": 5.0}
    assert doc["zones"]["parent"]["Canterbury far"] == "Canterbury" and doc["zones"]["splits"][0]["divide_km"] == 15
    hist = S.load_history(zoned, "ecmwf")
    close = next(h for h in hist if h["status"] == "closed")
    assert close["region"] == "Canterbury" and (close["factor"], close["version"]) == (1.0, 0)
    assert close["previous"]["factor"] == 0.85 and close["previous"]["version"] == 1
    assert close["replaced_by"] == ["Canterbury near", "Canterbury far"] and "rf-t26.16" in close["reason"]
    assert doc["closed"]["Canterbury"]["record"] == close["seq"]
    new = [h for h in hist if h["seq"] > close["seq"] and h["parent"] == "Canterbury"]
    assert [(h["region"], h["version"], h["parent"]) for h in new] == [("Canterbury near", 1, "Canterbury"),
                                                                       ("Canterbury far", 1, "Canterbury")]
    assert [h["seq"] for h in hist] == list(range(1, len(hist) + 1))
    # the saved buffers moved to the zones: each catchment scaled by its zone's factor
    for kind, raw in (("observed", 2.0), ("forecast", 0.5)):
        b = HourlyBuffer.load(zoned.buffer_path(kind, "ecmwf"))
        s = next(s for s in range(24) if b.hour[s] >= 0)
        assert b.regions == ("Auckland", "Canterbury near", "Canterbury far", "Tasman", "West Coast")
        assert b.scaled[s].tolist() == pytest.approx([raw, raw * 1.2, raw * 0.8, raw, raw * 1.5625])
    # rain ecmwf builds its buffers per zone too
    rd = FakeReader("fake", floor_run(NOW - timedelta(days=3)), NOW + timedelta(days=1))
    rep = pl.run_ecmwf(zoned, reader=rd, now=NOW + timedelta(days=1), log=lambda m: None)
    assert rep["observed"]["scale"]["zones"]["zones"][1] == "Canterbury near"
    b = HourlyBuffer.load(zoned.buffer_path("observed", "ecmwf"))
    assert np.allclose(b.scaled[b.hour >= 0], [1.0, 1.2, 0.8, 1.0, 1.5625])
    # the totals say which zones' factors reached a reach
    tot = pl.run_totals(zoned, now=NOW + timedelta(days=1), log=lambda m: None)
    p = tot.provenance(2, "observed", 1)[0]
    assert p["factor_version"] == {"Canterbury far": 1} and p["factor"] == {"Canterbury far": pytest.approx(0.8)}
    assert tot.kinds["observed"].info["zones"][2] == "Canterbury far"
    # back to one Canterbury zone: the zones are closed, Canterbury continues above its old versions
    doc = _scale(plain, NOW + timedelta(days=2))
    assert doc["regions"]["Canterbury"]["version"] == 2 and doc["regions"]["Canterbury"]["factor"] == 0.85
    assert set(doc["closed"]) == {"Canterbury near", "Canterbury far"}
    assert HourlyBuffer.load(plain.buffer_path("observed", "ecmwf")).regions == ZREG


def test_rezone_resets_the_scale_until_factors_are_applied():
    b = HourlyBuffer(3, "observed", ("A", "B"), np.array([0, 1, 1]))
    b.put(10, np.array([1.0, 2.0, 3.0]), "ECMWF_OBS", run_hour=7, lead=3,
          factors=ScaleFactors("ecmwf", ("A", "B"), [1.0, 2.0], [0, 4]))
    assert b.scaled[10].tolist() == [1.0, 4.0, 6.0]
    assert not b.rezone(("A", "B"), [0, 1, 1])
    assert b.rezone(("A", "B1", "B2"), [0, 1, 2])
    assert b.scaled[10].tolist() == [1.0, 2.0, 3.0] and b.factor_version.shape == (24, 3) and not b.factor_version.any()
    b.apply_factors(ScaleFactors("ecmwf", ("A", "B1", "B2"), [1.0, 1.5, 0.5], [0, 1, 1]))
    assert b.scaled[10].tolist() == [1.0, 3.0, 1.5]
    with pytest.raises(ValueError):
        b.rezone(("A",), [0, 1, 2])

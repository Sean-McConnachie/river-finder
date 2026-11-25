"""Storm validation of the gauge scale factors (rf-t26.7) on synthetic data: metrics, timing, catchment weights
and membership, IDW, catchment specs, and the whole ``rain validate`` step (factor variants, catchments, report)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from river_finder.rainfall import CatchmentWeights, GridSpec, Network, hour_index, hour_time
from river_finder.rainfall import validate as V
from river_finder.rainfall.config import ScalingConfig, parse_config
from river_finder.rainfall.gauges import RAW, GaugeHours, to_nztm
from river_finder.rainfall.scaling import FACTORS_FORMAT, CellStore

GRID = GridSpec("ecmwf", lon0=170.0, lat0=-40.0, dlon=0.1, dlat=-0.1, nx=4, ny=3)
NOW_H = hour_index(datetime(2026, 11, 22, 3, tzinfo=UTC))
HOURS = 900
EV_FIRST, EV_LAST = NOW_H - 300 + 1, NOW_H - 300 + 24     # the event: 24 h
MARGIN = 6
GAUGE_CELLS = (0, 1, 2, 4, 5)
IN_EVENT, OUTSIDE = 1.5, 1.2                               # gauge / ECMWF in and outside the event


def _label(h):
    return hour_time(h).strftime("%Y%m%d%H")


def _grid_rain(h: int) -> float:
    """ECMWF mm/h at every cell: rain every third hour, varying, heavier in the event."""
    base = float((h % 3 == 0) * (1 + h % 7))
    return 4.0 * (1 + h % 5) if EV_FIRST <= h <= EV_LAST else base


def _cfg(root):
    return parse_config({"observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
                         "grids": {"ecmwf": {k: getattr(GRID, k) for k in ("lon0", "lat0", "dlon", "dlat", "nx",
                                                                           "ny")}},
                         "paths": {"nz_data": str(root)},
                         "gauges": {"councils": {"wcrc": {"name": "West Coast Regional Council",
                                                          "base_url": "https://w/", "hts": ["data.hts"],
                                                          "region": "West Coast"}}}})


def _cell_lonlat(c):
    return GRID.lons[c % GRID.nx], GRID.lats[c // GRID.nx]


def _gauges() -> GaugeHours:
    hour = np.arange(NOW_H - HOURS + 1, NOW_H + 1, dtype=np.int64)
    G = len(GAUGE_CELLS)
    e = np.array([_grid_rain(int(h)) for h in hour], np.float32)
    ratio = np.where((hour >= EV_FIRST) & (hour <= EV_LAST), IN_EVENT, OUTSIDE).astype(np.float32)
    mm = np.tile(e * ratio, (G, 1))
    lon = np.array([_cell_lonlat(c)[0] + 0.01 for c in GAUGE_CELLS])
    lat = np.array([_cell_lonlat(c)[1] - 0.01 for c in GAUGE_CELLS])
    x, y = to_nztm(lon, lat)
    cols = {"id": np.array([f"wcrc:G{c}" for c in GAUGE_CELLS]), "council": np.array(["wcrc"] * G),
            "name": np.array([f"G{c}" for c in GAUGE_CELLS]), "lon": lon, "lat": lat, "x": x, "y": y,
            "cell": np.array(GAUGE_CELLS, np.int32), "cell_row": np.array([c // 4 for c in GAUGE_CELLS], np.int32),
            "cell_col": np.array([c % 4 for c in GAUGE_CELLS], np.int32), "region_of": np.zeros(G, np.int16)}
    meta = {"created": "2026-11-22T03:10:00Z", "grid": dict(GRID.to_dict(), cell="row * nx + col")}
    return GaugeHours(hour, mm, np.full((G, HOURS), -1, np.int16), np.full((G, HOURS), RAW, np.uint8),
                      np.zeros((G, HOURS), np.uint8), cols, ("West Coast",), meta)


def _store(first, last, cells):
    s = CellStore.empty("ecmwf", GRID)
    s.ensure(cells, first, last)
    s.mm[:] = np.array([_grid_rain(h) for h in range(first, last + 1)], np.float32)[:, None]
    s.src[:] = s.reader_code("openmeteo-s3")
    s.run_hour[:] = np.arange(first, last + 1)[:, None] - 3
    s.lead[:] = 3
    return s


def _raster(path, x0, y0, values):
    """A tiny local_catchment_core.tif (EPSG:2193, 8 m pixels) with ``values`` (rows, cols) from (x0, y0) top-left."""
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    v = np.asarray(values, np.int64)
    with rasterio.open(path, "w", driver="GTiff", width=v.shape[1], height=v.shape[0], count=1, dtype="int64",
                       crs="EPSG:2193", transform=from_origin(x0, y0, 8.0, 8.0), nodata=-1) as ds:
        ds.write(v, 1)


def _data(root):
    """Gauges, both ECMWF stores, a factor file, weights + network of 4 catchments (rows 0, 1 -> 2, outlet; row 3
    apart), a segment table and a tile raster holding gauges G0 and G1 in rows 0 and 1."""
    cfg = _cfg(root)
    g = _gauges()
    g.save(cfg.gauges_dir / "gauges_hourly.npz")
    _store(NOW_H - 719, NOW_H, np.arange(GRID.size)).save(cfg.cells_path("ecmwf"))
    _store(EV_FIRST - MARGIN, EV_LAST + MARGIN, np.arange(GRID.size)).save(
        V.store_path(cfg, EV_FIRST - MARGIN, EV_LAST + MARGIN))
    doc = {"format": FACTORS_FORMAT, "family": "ecmwf", "updated": "2026-11-22T04:00:00Z",
           "regions": {"West Coast": {"factor": 1.2, "version": 1, "set": "2026-11-22T04:00:00Z", "status": "scaled"}},
           "last_update": {"window": {"first_hour": V._hour(NOW_H - 719), "last_hour": V._hour(NOW_H)},
                           "regions": {"West Coast": {"ratio": 1.21}}}}
    cfg.factors_path("ecmwf").write_text(json.dumps(doc))
    gid = np.array([7, 8, 9, 10], np.int64)
    rows, cells = np.array([0, 1, 1, 2, 3]), np.array([0, 1, 5, 5, 11])
    counts = sp.csr_matrix((np.array([2, 1, 1, 1, 3], np.int32), (rows, cells)), shape=(4, GRID.size))
    npix = np.array([2, 2, 1, 3], np.int64)
    down = np.array([2, 2, -1, -1], np.int64)
    w = CatchmentWeights(GRID, counts, gid, npix, 1e6, down, np.zeros(4, np.int16), ("West Coast",),
                         np.ones(4, np.float32), np.ones(4, np.float32), {"built": "x"})
    w.save(cfg.weights_path("ecmwf"))
    Network.build(gid, down, npix * 1e6, np.zeros(4, np.int16), ("West Coast",), np.array([2, 2, 5, 3], np.float32),
                  np.zeros(4, np.int32)).save(cfg.network_path)
    pd.DataFrame({"gid": gid, "region": ["West Coast"] * 4, "rnorm": ["test", "test", "test", "other"],
                  "river": ["Test River", "Test River", "Test River", "Other Stream"],
                  "area_km2": [2.0, 2.0, 5.0, 3.0]}).to_pickle(cfg.segments_path)
    x, y = np.asarray(g.x), np.asarray(g.y)
    # one raster around G0 (gid 7) and one around G1 (nodata at G1 itself: the nearest pixel is gid 8)
    _raster(cfg.tiles_dir / "a" / "local_catchment_core.tif", x[0] - 40, y[0] + 40, np.full((10, 10), 7))
    v = np.full((10, 10), 8)
    v[4:7, 4:7] = -1
    _raster(cfg.tiles_dir / "b" / "local_catchment_core.tif", x[1] - 41, y[1] + 41, v)
    return cfg, w


# --- pieces -----------------------------------------------------------------------------------------------
def test_parse_event_and_slug():
    a, b = V.parse_event("2026111412", "2026111712")
    assert hour_time(a) == datetime(2026, 11, 14, 13, tzinfo=UTC) and b - a + 1 == 72
    with pytest.raises(SystemExit):
        V.parse_event("2026111712", "2026111412")
    assert V.slug(["West Coast", "Canterbury divide"]) == "west-coast-canterbury-divide"


def test_error_metrics():
    m = V.error_metrics([90.0, 220.0, np.nan], [100.0, 200.0, 50.0])
    assert m["n"] == 2 and m["bias_mm"] == 5.0 and m["model_over_gauge"] == pytest.approx(310 / 300, abs=1e-3)
    assert m["rmse_mm"] == pytest.approx(np.sqrt((100 + 400) / 2), abs=0.05) and m["mae_mm"] == 15.0
    assert m["rms_log"] == pytest.approx(np.sqrt((np.log(0.9) ** 2 + np.log(1.1) ** 2) / 2), abs=1e-3)
    assert m["median_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert V.error_metrics([np.nan], [1.0]) == {"n": 0}


def test_timing_finds_a_late_model():
    T, pad = 48, 6
    x = np.zeros(T)
    x[20:24] = [2, 8, 4, 1]
    y = np.zeros(T + 2 * pad)
    y[pad + 22:pad + 26] = [2, 8, 4, 1]                  # the model 2 h late
    t = V.timing(x, y, first=hour_index(datetime(2026, 11, 15, tzinfo=UTC)), pad=pad)
    assert t["best_lag_h"] == 2 and t["r_best"] == pytest.approx(1.0) and t["r0"] < 0.5
    assert t["peak_lag_h"] == 2 and t["centroid_lag_h"] == pytest.approx(2.0)
    assert t["gauge_peak_mm"] == 8.0 and t["gauge_peak_hour"] == "2026-11-15 21:00Z"
    same = V.timing(x, np.concatenate([np.zeros(pad), x, np.zeros(pad)]), first=0, pad=pad)
    assert same["best_lag_h"] == 0 and same["r3"] == pytest.approx(1.0) and same["centroid_lag_h"] == 0
    with pytest.raises(ValueError):
        V.timing(x, x, first=0, pad=pad)


def test_upstream_mask_and_catchment_cells(tmp_path):
    cfg, w = _data(tmp_path)
    net = Network.load(cfg.network_path)
    assert V.upstream_mask(net, 2).tolist() == [True, True, True, False]
    assert V.upstream_mask(net, 0).tolist() == [True, False, False, False]
    zone_of = np.array([0, 1, 1, 0], np.int16)
    cc = V.catchment_cells(w, V.upstream_mask(net, 2), zone_of, 2)
    assert cc["area_km2"] == pytest.approx(5.0)
    u = cc["u"]
    assert u.sum() == pytest.approx(1.0) and cc["zone_share"].tolist() == pytest.approx([0.4, 0.6])
    assert u[0, 0] == pytest.approx(0.4) and u[1, 1] == pytest.approx(0.2) and u[1, 5] == pytest.approx(0.4)
    # the same mean depth as the upstream totals: sum of local volumes over the upstream pixel area
    field = np.arange(GRID.size, dtype=float)
    local = w.mm(field).astype(float) * w.area_m2
    assert u.sum(axis=0) @ field == pytest.approx(local[:3].sum() / net.up_area_m2[2])


def test_idw_weights():
    w = V.idw_weights([0.0, 50_000.0], [0.0, 0.0], [10_000.0, -10_000.0, 100_000.0], [0.0, 0.0, 0.0])
    assert w.sum(axis=1) == pytest.approx([1.0, 1.0])
    assert w[0, 0] == pytest.approx(w[0, 1]) and w[0, 2] > 0          # 3 nearest at least, beyond 30 km too
    w2 = V.idw_weights([0.0], [0.0], [100.0, 20_000.0, 25_000.0, 29_000.0], [0.0] * 4, min_gauges=1)
    assert w2[0, 0] == pytest.approx(1 / (1 + 1 / 400 + 1 / 625 + 1 / 841))   # under 1 km counts as 1 km
    assert V.idw_weights([0.0], [0.0], [], []).shape == (1, 0)


def test_locate_gauges_and_resolve_catchments(tmp_path):
    cfg, _ = _data(tmp_path)
    g = GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    gid = V.locate_gauges(cfg.tiles_dir, g.x, g.y)
    assert gid.tolist() == [7, 8, -1, -1, -1]          # G1 sits on no-data: the nearest pixel is taken
    assert V.locate_gauges(cfg.tiles_dir, [np.nan], [np.nan]).tolist() == [-1]
    table = pd.read_pickle(cfg.segments_path)
    r = V.resolve_catchments(["test", "Mine=10", "8"], table, ["West Coast"])
    assert [(c["name"], c["row"], c["gid"]) for c in r] == [("Test", 2, 9), ("Mine", 3, 10), ("Test River", 1, 8)]
    with pytest.raises(SystemExit):
        V.resolve_catchments(["nowhere"], table, ["West Coast"])
    with pytest.raises(SystemExit):
        V.resolve_catchments(["99"], table, ["West Coast"])


def test_leave_out_factor():
    sc = ScalingConfig()
    fz = {"leo": {"factor": 1.1}, "_leo_used": [{"id": k, "gauge_mm": gm, "grid_mm": 100.0}
                                               for k, gm in (("a", 100.0), ("b", 120.0), ("c", 140.0), ("d", 400.0))]}
    assert V.leave_out_factor(fz, ["x"], sc) == 1.1                   # none of its gauges: the no-event factor
    assert V.leave_out_factor(fz, ["d"], sc) == pytest.approx(1.2)
    assert V.leave_out_factor(fz, ["c", "d"], sc) == 1.0              # 2 left (< min_gauges)
    fz["_leo_used"][3]["gauge_mm"] = 1000.0
    assert V.leave_out_factor(fz, ["a"], sc) == 2.0                   # clamped


# --- the whole step ----------------------------------------------------------------------------------------
def test_run_validate_factors_gauges_catchments_and_report(tmp_path):
    cfg, w = _data(tmp_path)
    rep = V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), regions=["West Coast"], catchments=["test"],
                         fetch=False, margin=MARGIN, log=lambda m: None)
    assert rep["event"]["hours"] == 24 and rep["zones"] == ["West Coast"]
    f = rep["zone_reports"]["West Coast"]["factors"]
    e_ev = sum(_grid_rain(h) for h in range(EV_FIRST, EV_LAST + 1))
    e_all = sum(_grid_rain(h) for h in range(NOW_H - 719, NOW_H + 1))
    assert f["in_use"]["factor"] == 1.2 and f["in_use"]["version"] == 1
    assert f["window"]["factor"] == pytest.approx((OUTSIDE * (e_all - e_ev) + IN_EVENT * e_ev) / e_all, abs=1e-4)
    assert f["leo"]["factor"] == pytest.approx(OUTSIDE) and f["leo"]["hours"] == 720 - 24
    assert f["event"]["factor"] == pytest.approx(IN_EVENT)
    gs = rep["gauges"]
    assert len(gs) == 5 and all(r["gauge_over_raw"] == pytest.approx(IN_EVENT, abs=1e-3) for r in gs)
    assert all(r["logo_factor"] == pytest.approx(OUTSIDE) for r in gs)
    assert all(r["timing"]["r0"] == pytest.approx(1.0) and r["timing"]["best_lag_h"] == 0 for r in gs)
    m = rep["zone_reports"]["West Coast"]["metrics"]
    assert m["raw"]["model_over_gauge"] == pytest.approx(1 / IN_EVENT, abs=1e-3)
    assert m["leo"]["model_over_gauge"] == pytest.approx(OUTSIDE / IN_EVENT, abs=1e-3)
    assert m["event"]["bias_mm"] == 0.0 and m["event"]["rms_log"] == pytest.approx(0.0, abs=1e-3)
    assert rep["ecmwf_check"]["event_over_scaling"] == 1.0
    c = rep["catchments"][0]
    assert (c["name"], c["gid"], c["area_km2"]) == ("Test", 9, 5.0) and c["zones"] == {"West Coast": 1.0}
    assert [x["id"] for x in c["gauges_inside"]] == ["wcrc:G0", "wcrc:G1"]
    assert c["model"]["raw"] == pytest.approx(e_ev, abs=0.1)                  # uniform rain: the cell value
    assert c["model"]["leo"] == pytest.approx(OUTSIDE * e_ev, abs=0.1)
    assert c["model"]["loco"] == pytest.approx(OUTSIDE * e_ev, abs=0.1)       # 3 gauges left: the same ratio
    for est in ("inside", "idw", "guided"):
        assert c["gauge"][est] == pytest.approx(IN_EVENT * e_ev, abs=0.2)
    assert c["timing"]["r0"] == pytest.approx(1.0)
    assert rep["catchment_metrics"]["idw"]["event"]["bias_mm"] == pytest.approx(0.0, abs=0.2)
    out = cfg.rain_dir / "validation" / f"validate_west-coast_{_label(EV_FIRST - 1)}_{_label(EV_LAST)}"
    saved = json.loads(out.with_suffix(".json").read_text())
    assert saved["format"] == V.FORMAT and saved["catchments"][0]["gid"] == 9
    md = out.with_suffix(".md").read_text()
    assert "### West Coast" in md and "### Catchments" in md and "| wcrc:G0 |" in md

    # a gauge left out of everything; an unknown one is refused
    rep2 = V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), catchments=["test"], fetch=False,
                          margin=MARGIN, exclude=["wcrc:G0"], log=lambda m: None)
    assert len(rep2["gauges"]) == 4 and rep2["excluded_gauges"] == ["wcrc:G0"]
    assert [x["id"] for x in rep2["catchments"][0]["gauges_inside"]] == ["wcrc:G1"]
    with pytest.raises(SystemExit):
        V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), fetch=False, margin=MARGIN, exclude=["wcrc:Z"],
                       log=lambda m: None)


def test_run_validate_leaves_out_a_gauge_that_under_read_the_storm(tmp_path):
    cfg, _ = _data(tmp_path)
    p = cfg.gauges_dir / "gauges_hourly.npz"
    g = GaugeHours.load(p)
    ev = np.flatnonzero((g.hour >= EV_FIRST) & (g.hour <= EV_LAST))
    g.mm[4, ev] = 0.0
    g.mm[4, ev[::6]] = 0.5               # wcrc:G5 tips now and then: 2 mm of a ~430 mm storm, never dry for 24 h
    g.save(p)
    kw = dict(regions=["West Coast"], catchments=["test"], fetch=False, margin=MARGIN, log=lambda m: None)
    rep = V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), **kw)
    assert len(rep["gauges"]) == 4 and "wcrc:G5" not in {r["id"] for r in rep["gauges"]} and rep["underread_rule"]
    cl = rep["cleaning"]
    assert [x["id"] for x in cl["underreads_in_event"]] == ["wcrc:G5"] and cl["underreads_in_event"][0]["zone"] == \
        "West Coast"
    assert cl["rule"]["underread"]["fraction"] == 0.2 and not cl["outages_in_event"]
    few = rep["zone_reports"]["West Coast"]["excluded"]["few_hours"]
    assert [x["id"] for x in few] == ["wcrc:G5"] and few[0]["underread_hours"] >= 24
    md = (cfg.rain_dir / "validation" / f"validate_west-coast_{_label(EV_FIRST - 1)}_{_label(EV_LAST)}.md").read_text()
    assert "Under-read events (rf-t26.20) in the storm, left out: wcrc:G5" in md and "wcrc:G5 (under-read" in md
    f = rep["zone_reports"]["West Coast"]["factors"]
    assert f["event"]["factor"] == pytest.approx(IN_EVENT) and f["leo"]["factor"] == pytest.approx(OUTSIDE)
    # without the rule (as before rf-t26.20) the dead gauge stays and drags the storm's ratio down
    rep0 = V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), underread=False, **kw)
    assert "wcrc:G5" in {r["id"] for r in rep0["gauges"]} and not rep0["cleaning"]["underreads_in_event"]
    assert rep0["zone_reports"]["West Coast"]["factors"]["event"]["factor"] == pytest.approx(0.8 * IN_EVENT, abs=0.01)
    stem = f"validate_west-coast_{_label(EV_FIRST - 1)}_{_label(EV_LAST)}_no-underread"
    assert "Under-read rule (rf-t26.20) off" in (cfg.rain_dir / "validation" / f"{stem}.md").read_text()


def test_run_validate_refuses_events_outside_the_cache_and_a_missing_store(tmp_path):
    cfg, _ = _data(tmp_path)
    with pytest.raises(SystemExit, match="not inside"):
        V.run_validate(cfg, _label(NOW_H - 2000), _label(NOW_H - 1900), fetch=False, log=lambda m: None)
    with pytest.raises(SystemExit, match="without --no-fetch"):
        V.run_validate(cfg, _label(EV_FIRST - 1), _label(EV_LAST), fetch=False, margin=3, log=lambda m: None)


def test_event_store_fills_from_readers(tmp_path):
    from datetime import timedelta

    from river_finder.rainfall.ecmwf import Reader, floor_run
    from river_finder.rainfall.fetch import Bucket, Http

    class Fake(Reader):
        name = "openmeteo-s3"

        def __init__(self):
            super().__init__(GRID)
            self.bucket = Bucket(["https://nowhere/"], Http(retries=0))
            self.reads = 0

        def _list_leads(self, run):
            return tuple(range(1, 19)) if run <= hour_time(NOW_H) - timedelta(hours=6) else ()

        def hourly(self, run, lead):
            self.reads += 1
            return np.full(GRID.shape, float(lead), np.float32), "fake"

    cfg = _cfg(tmp_path)
    rd = Fake()
    s, path = V.event_store(cfg, EV_FIRST, EV_LAST, readers=[rd], log=lambda m: None)
    assert path.exists() and rd.reads == 24 and np.isfinite(s.values(np.arange(GRID.size), EV_FIRST, EV_LAST)).all()
    lead = s.lead[:, 0]
    assert lead.min() >= 1 and lead.max() <= 6                      # each hour from the newest run: leads 1-6
    assert floor_run(hour_time(EV_FIRST - 1)) <= hour_time(int(s.run_hour[0, 0]))
    s2, _ = V.event_store(cfg, EV_FIRST, EV_LAST, readers=[rd], log=lambda m: None)   # held: nothing read again
    assert rd.reads == 24 and s2.n_hours == 24


def test_cli_rain_validate(tmp_path):
    from river_finder import cli

    cfg, _ = _data(tmp_path)
    toml = tmp_path / "rain.toml"
    toml.write_text(f"""
[observed]
source = "ecmwf"
[forecast]
source = "ecmwf"
[paths]
nz_data = "{tmp_path}"
[grids.ecmwf]
lon0 = {GRID.lon0}
lat0 = {GRID.lat0}
dlon = {GRID.dlon}
dlat = {GRID.dlat}
nx = {GRID.nx}
ny = {GRID.ny}
[gauges.councils.wcrc]
name = "West Coast Regional Council"
base_url = ["https://w/"]
hts = ["data.hts"]
region = "West Coast"
""")
    out = tmp_path / "report"
    cli.main(["rain", "validate", "--config", str(toml), "--data", str(tmp_path), "--event", _label(EV_FIRST - 1),
              _label(EV_LAST), "--catchment", "test", "--margin", str(MARGIN), "--no-fetch", "--out", str(out)])
    assert (out / f"validate_west-coast_{_label(EV_FIRST - 1)}_{_label(EV_LAST)}.md").exists()
    with pytest.raises(SystemExit):
        cli.main(["rain", "validate", "--config", str(toml), "--data", str(tmp_path), "--no-fetch"])

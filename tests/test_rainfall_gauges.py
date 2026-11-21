"""Rain gauges (rf-t26.4): Hilltop XML, time zones (NZST / NZ wall clock across DST), hourly aggregation,
quality states, placement, cache merge, the per-site choice of rain series, ECan's Open Data Portal, and whole
runs against fake servers.

Fixtures: tests/fixtures/hilltop/{error,getdata_raw,getdata_total,measurement_list,quality_series,site_list,
site_measurements,status}.xml are SYNTHETIC (hand-written from the Hilltop Server documentation and hilltop-py's
parsers before the servers could be reached). tests/fixtures/hilltop/{wcrc_site_measurements,tdc_site_measurements,
wcrc_getdata_raw}.xml and tests/fixtures/ecan/*.csv are REAL answers of 2026-11-21 (each file says from where;
the TDC one and the ECan site list are trimmed).
"""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np
import pytest

from river_finder.rainfall import ecanportal as E
from river_finder.rainfall import gauges as G
from river_finder.rainfall import hilltop as H
from river_finder.rainfall.config import CONFIG_PATH, load_config, parse_config
from river_finder.rainfall.fetch import FetchError, Http
from river_finder.rainfall.grid import hour_index

FIX = Path(__file__).parent / "fixtures" / "hilltop"
ECAN = Path(__file__).parent / "fixtures" / "ecan"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def hr(*a) -> int:
    return hour_index(utc(*a))


# --- XML ------------------------------------------------------------------------------------------
def test_measurement_names_and_choice():
    names = H.parse_measurement_names(fx("measurement_list.xml"))
    assert names == ["Flow", "Rainfall Intensity", "Rainfall [Rainfall]", "Stage"]  # repeats dropped
    assert H.choose_measurement(names, ("Rainfall",)) == "Rainfall [Rainfall]"  # bracketed data source
    assert H.choose_measurement(names, ("rainfall intensity", "Rainfall")) == "Rainfall Intensity"  # hint order
    assert H.choose_measurement(["Rainfall Total", "Rainfall"], ("Rainfall Total", "Rainfall")) == "Rainfall Total"
    assert H.choose_measurement(["Flow", "Stage"], ("Rainfall",)) is None
    assert H.rain_like(names) == ["Rainfall Intensity", "Rainfall [Rainfall]"]


def test_site_list_locations():
    sites = H.parse_site_list(fx("site_list.xml"))
    by = {s.name: s for s in sites}
    assert list(by) == ["Grey & Ahaura at Test Bridge", "Ōtira at Test Pass", "Unlocated Test Gauge",
                        "Zero Test Gauge", "Chatham Test Gauge"]
    assert by["Grey & Ahaura at Test Bridge"].lat == pytest.approx(-42.4504)
    assert by["Grey & Ahaura at Test Bridge"].lon == pytest.approx(171.2108)
    assert not by["Unlocated Test Gauge"].located
    assert not by["Zero Test Gauge"].located  # 0, 0 is "no location"
    assert by["Chatham Test Gauge"].lon == pytest.approx(183.44)  # west longitudes wrapped to 0..360


def test_site_measurements_and_source():
    srcs = H.parse_site_measurements(fx("site_measurements.xml"))
    assert [(s.datasource, s.tstype) for s in srcs] == [("Rainfall", "StdQualSeries"), ("Rainfall", "StdSeries"),
                                                        ("Stage", "StdSeries")]
    src = H.find_source(srcs, "Rainfall [Rainfall]")
    assert src.tstype == "StdSeries" and src.units == "mm" and src.request_name == "Rainfall [Rainfall]"
    assert src.first == datetime(1995, 6, 23) and src.last == datetime(2026, 11, 20, 18)
    assert H.find_source(srcs, "Rainfall").tstype == "StdSeries"
    assert H.find_source(srcs, "Flow") is None


def test_getdata_records_gaps_quality():
    s = H.parse_data(fx("getdata_raw.xml"))
    assert s.site == "Grey & Ahaura at Test Bridge" and s.units == "mm" and s.interpolation == "Incremental"
    assert len(s) == 7 and s.times[0] == datetime(2026, 11, 20, 13, 15)
    assert s.values.tolist() == [0.5, 0.5, 0.5, 0.5, 0.5, -0.5, 0.5]
    assert s.quality.tolist() == [600, 600, 600, 200, 200, 200, 200]
    assert s.gap_before.tolist() == [False, False, False, False, True, False, False] and not s.gap_at_end
    q = H.parse_data(fx("quality_series.xml"))
    assert q.tstype == "StdQualSeries" and q.values.tolist() == [600, 400, 100]


def test_errors_and_hostile_xml():
    with pytest.raises(H.HilltopError, match="No data for site Missing Test Gauge"):
        H.parse_data(fx("error.xml"))
    with pytest.raises(H.HilltopError, match="not XML"):
        H.parse_root(b"<html><body>404 Not Found</body>")
    with pytest.raises(H.HilltopError, match="DTD"):
        H.parse_root(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><HilltopServer>&a;</HilltopServer>')
    # bytes that are not the UTF-8 the declaration claims (cp1252 macron-less O-tilde) still parse
    body = '<?xml version="1.0" encoding="UTF-8"?><HilltopServer><Site Name="\xd5tira"/></HilltopServer>'
    assert [s.name for s in H.parse_site_list(body.encode("cp1252"))] == ["Õtira"]
    assert H.parse_data(b"<Hilltop><Agency>x</Agency></Hilltop>").times == []  # no Measurement: no records
    with pytest.raises(H.HilltopError, match="unreadable record time"):
        H.parse_data(b"<Hilltop><Measurement><Data><E><T>20/11/2026 1pm</T><I1>1</I1></E></Data></Measurement>"
                     b"</Hilltop>")
    st = H.parse_status(fx("status.xml"))
    assert st["Agency"] == "Example Regional Council" and len(st["data_files"]) == 2


def test_build_url():
    u = H.build_url("https://h.example/", "data.hts", "GetData", Site="Grey & Ahaura", Measurement="Rainfall",
                    Interval="1 hour", Missing=None)
    assert u == ("https://h.example/data.hts?Service=Hilltop&Request=GetData&Site=Grey%20%26%20Ahaura"
                 "&Measurement=Rainfall&Interval=1%20hour")


# --- time zones -----------------------------------------------------------------------------------
def test_nzst_fixed_offset_and_explicit_offsets():
    t = [datetime(2026, 1, 15, 12), datetime(2026, 4, 5, 2, 30), datetime(2026, 7, 1, 0, 0)]
    s, st = H.to_utc(t, "NZST")
    assert [datetime.fromtimestamp(x, UTC) for x in s] == [utc(2026, 1, 15, 0), utc(2026, 4, 4, 14, 30),
                                                           utc(2026, 6, 30, 12)]
    s, st = H.to_utc([H.parse_time("2026-11-20T13:15:00+12:00"), H.parse_time("2026-11-20T01:15:00Z"),
                      H.parse_time("2026-11-20 01:15:00.500")], "NZST")
    assert st.explicit_offset == 2
    assert s[0] == s[1] == utc(2026, 11, 20, 1, 15).timestamp()
    assert s[2] == utc(2026, 11, 19, 13, 15).timestamp()  # naive: NZST; fraction floored
    assert H.to_utc([datetime(2026, 11, 20, 1)], "UTC")[0][0] == utc(2026, 11, 20, 1).timestamp()


def test_wall_clock_dst_end_repeated_hour():
    """5 April 2026: 03:00 NZDT -> 02:00 NZST, so 02:00-02:59 happens twice; records arrive in time order."""
    wall = [datetime(2026, 4, 5, h, m) for h, m in ((1, 30), (2, 0), (2, 30), (2, 0), (2, 30), (3, 0), (3, 30))]
    s, st = H.to_utc(wall, "Pacific/Auckland")
    assert [datetime.fromtimestamp(x, UTC) for x in s] == [utc(2026, 4, 4, 12, 30), utc(2026, 4, 4, 13),
                                                           utc(2026, 4, 4, 13, 30), utc(2026, 4, 4, 14),
                                                           utc(2026, 4, 4, 14, 30), utc(2026, 4, 4, 15),
                                                           utc(2026, 4, 4, 15, 30)]
    assert np.all(np.diff(s) == 1800)
    assert (st.ambiguous, st.second_pass, st.nonexistent) == (4, 2, 0)
    # a lone record in the repeated hour cannot be placed: the first pass (NZDT)
    s, st = H.to_utc([datetime(2026, 4, 5, 2, 15)], "Pacific/Auckland")
    assert s[0] == utc(2026, 4, 4, 13, 15).timestamp() and st.ambiguous == 1 and st.second_pass == 0


def test_wall_clock_dst_start_skipped_hour():
    """27 September 2026: 02:00 NZST -> 03:00 NZDT; 02:xx does not exist on a wall clock (read as NZST)."""
    wall = [datetime(2026, 9, 27, 1, 30), datetime(2026, 9, 27, 3, 0), datetime(2026, 9, 27, 3, 30)]
    s, st = H.to_utc(wall, "Pacific/Auckland")
    assert [datetime.fromtimestamp(x, UTC) for x in s] == [utc(2026, 9, 26, 13, 30), utc(2026, 9, 26, 14),
                                                           utc(2026, 9, 26, 14, 30)]
    s, st = H.to_utc([datetime(2026, 9, 27, 2, 30)], "Pacific/Auckland")
    assert s[0] == utc(2026, 9, 26, 14, 30).timestamp() and st.nonexistent == 1
    # summer and winter away from the changes
    s, _ = H.to_utc([datetime(2026, 1, 15, 13), datetime(2026, 7, 15, 12)], "Pacific/Auckland")
    assert s.tolist() == [utc(2026, 1, 15).timestamp(), utc(2026, 7, 15).timestamp()]


def test_request_times_in_server_zone():
    t = int(utc(2026, 1, 15, 0).timestamp())
    assert H.local_time(t, "NZST") == "2026-01-15T12:00:00"
    assert H.local_time(t, "Pacific/Auckland") == "2026-01-15T13:00:00"
    assert H.local_time(t, "UTC") == "2026-01-15T00:00:00"
    assert H.local_time(int(utc(2026, 4, 4, 14, 30).timestamp()), "Pacific/Auckland") == "2026-04-05T02:30:00"


def test_hours_across_dst_end_not_double_counted():
    """Half-hourly 0.5 mm on the NZ wall clock through the repeated hour: every UTC hour gets exactly 1.0 mm."""
    hm = [(0, 30), (1, 0), (1, 30), (2, 0), (2, 30),               # NZDT
          (2, 0), (2, 30), (3, 0), (3, 30), (4, 0), (4, 30), (5, 0)]  # the clock went back: NZST
    wall = [datetime(2026, 4, 5, h, m) for h, m in hm]
    s, st = H.to_utc(wall, "Pacific/Auckland")
    assert s[0] == utc(2026, 4, 4, 11, 30).timestamp() and s[-1] == utc(2026, 4, 4, 17).timestamp()
    assert np.all(np.diff(s) == 1800) and st.second_pass == 2
    first, last = hr(2026, 4, 4, 12), hr(2026, 4, 4, 17)
    b = G.hours_from_records(s, np.full(len(s), 0.5), first, last, rec_start=s[0] - 1800, rec_end=s[-1])
    assert b.mm.tolist() == [1.0] * 6 and set(b.state.tolist()) == {G.RAW}


# --- hours ----------------------------------------------------------------------------------------
def _raw_fixture_hours(with_quality: bool):
    s = H.parse_data(fx("getdata_raw.xml"))
    t, _ = H.to_utc(s.times, "NZST")
    src = H.find_source(H.parse_site_measurements(fx("site_measurements.xml")), "Rainfall")
    r0, r1 = (float(H.to_utc([x], "NZST")[0][0]) for x in (src.first, src.last))
    qual = None
    if with_quality:
        q = H.parse_data(fx("quality_series.xml"))
        qual = (H.to_utc(q.times, "NZST")[0], q.values.astype(np.int32))
    return G.hours_from_records(t, s.values, hr(2026, 11, 20, 1), hr(2026, 11, 20, 8), gap_before=s.gap_before,
                                gap_at_end=s.gap_at_end, rec_start=r0, rec_end=r1, rec_qc=s.quality, qual=qual)


def _names(flags: int) -> list[str]:
    return [n for b, n in G.FLAGS.items() if flags & b]


def test_raw_records_to_hours_ending():
    """Tips at 13:15, 13:40, 14:00 NZST -> hour ending 02:00Z (a record on the hour belongs to the hour it ends);
    the gap 14:20-17:10 NZST: hours 04Z and 05Z missing, 03Z and 06Z partial; the negative record: range;
    after the record's last time (18:00 NZST = 06Z): outside."""
    b = _raw_fixture_hours(with_quality=False)
    got = [(round(float(m), 2) if np.isfinite(m) else None, G.STATES[s], _names(f), int(q))
           for m, s, f, q in zip(b.mm, b.state, b.flags, b.qc, strict=True)]
    assert got == [(0.0, "raw", [], -1),                     # 01Z: inside the record, no tip: 0 mm
                   (1.5, "good", [], 600),                   # 02Z: three tips, QC 600 on each record
                   (0.5, "suspect", ["partial"], 200),       # 03Z: 14:20 tip, then the gap
                   (None, "missing", ["gap"], -1),           # 04Z
                   (None, "missing", ["gap"], -1),           # 05Z
                   (1.0, "suspect", ["partial", "range"], 200),  # 06Z: 17:10 + 18:00, the -0.5 dropped
                   (None, "missing", ["outside"], -1),       # 07Z: after the last record
                   (None, "missing", ["outside"], -1)]


def test_quality_series_applies_up_to_its_time():
    """Codes end their period: 600 up to 02Z, 400 to 03:30Z, 100 to 04Z, nothing after (not coded yet)."""
    b = _raw_fixture_hours(with_quality=True)
    assert b.qc.tolist()[:6] == [600, 600, 400, 100, -1, -1]
    assert [G.STATES[s] for s in b.state] == ["good", "good", "suspect", "missing", "missing", "suspect", "missing",
                                             "missing"]
    assert "low_qc" in _names(int(b.flags[2])) and "qc_missing" in _names(int(b.flags[3]))
    # an hour only partly coded is raw, not good
    qs = np.array([utc(2026, 11, 20, 1, 30).timestamp()], np.int64)
    one = G.hours_from_records(np.zeros(0, np.int64), np.zeros(0), hr(2026, 11, 20, 1), hr(2026, 11, 20, 2),
                               rec_start=0, rec_end=utc(2026, 11, 20, 3).timestamp(), qual=(qs, np.array([600])))
    assert one.qc.tolist() == [600, 600] and [G.STATES[s] for s in one.state] == ["good", "raw"]


def test_qc_state_classes():
    st, fl = G.qc_state([-1, 0, 100, 150, 200, 300, 400, 500, 600])
    assert [G.STATES[s] for s in st] == ["raw", "raw", "missing", "suspect", "raw", "suspect", "suspect", "good",
                                         "good"]
    assert fl.tolist() == [0, 0, G.F_QC_MISSING, G.F_LOW_QC, 0, G.F_LOW_QC, G.F_LOW_QC, 0, 0]


@pytest.mark.parametrize("method,first_hour", [("total-end", 1), ("total-start", 2)])
def test_server_totals(method, first_hour):
    s = H.parse_data(fx("getdata_total.xml"))
    t, _ = H.to_utc(s.times, "NZST")
    a = hr(2026, 11, 20, first_hour)
    b = G.hours_from_records(t, s.values, a, a + 4, rec_start=0, method=method, max_mm=200.0)
    assert [None if not np.isfinite(m) else float(m) for m in b.mm] == [0.0, 1.5, 0.5, None, 250.0]
    assert [G.STATES[x] for x in b.state] == ["raw", "raw", "raw", "missing", "suspect"]
    assert _names(int(b.flags[3])) == ["no_value"] and _names(int(b.flags[4])) == ["range"]


def test_no_records():
    a, z = hr(2026, 11, 20, 1), hr(2026, 11, 20, 3)
    empty = np.zeros(0, np.int64)
    # nothing known about the record: missing (outside)
    b = G.hours_from_records(empty, np.zeros(0), a, z)
    assert set(b.state.tolist()) == {G.MISSING} and set(b.flags.tolist()) == {G.F_OUTSIDE}
    # inside a known record, no tips: dry (0 mm)
    b = G.hours_from_records(empty, np.zeros(0), a, z, rec_start=0, rec_end=utc(2026, 11, 21).timestamp())
    assert b.mm.tolist() == [0.0, 0.0, 0.0] and set(b.state.tolist()) == {G.RAW}
    # a gap and nothing else: missing (gap)
    b = G.hours_from_records(empty, np.zeros(0), a, z, gap_at_end=True, rec_start=0,
                             rec_end=utc(2026, 11, 21).timestamp())
    assert set(b.flags.tolist()) == {G.F_GAP}


# --- cache ----------------------------------------------------------------------------------------
def _block(first: int, n: int, v: float) -> G.HourBlock:
    return G.HourBlock(first, np.full(n, v, np.float32), np.full(n, -1, np.int16), np.full(n, G.RAW, np.uint8),
                       np.zeros(n, np.uint8))


def test_merge_newer_wins_and_keep_window():
    m = G.merge(_block(10, 11, 1.0), _block(18, 8, 2.0))
    assert (m.first, m.last) == (10, 25)
    assert m.mm[:8].tolist() == [1.0] * 8 and m.mm[8:].tolist() == [2.0] * 8
    m = G.merge(_block(10, 3, 1.0), _block(20, 2, 2.0), keep_from=11)
    assert (m.first, m.last) == (11, 21)
    assert m.state[2:9].tolist() == [G.MISSING] * 7 and np.all(m.flags[2:9] == G.F_NOT_FETCHED)
    assert np.isnan(m.mm[2:9]).all()


def test_plan_fetches_only_missing_and_newest_hours():
    assert G.plan(None, 100, 147, 145) == [(100, 147)]
    full = _block(90, 60, 0.0)  # 90 .. 149
    assert G.plan(full, 100, 147, 145) == [(145, 147)]
    holed = G.merge(G.merge(_block(90, 15, 0.0), G.HourBlock.empty(105, 5)), _block(110, 40, 0.0))
    assert G.plan(holed, 100, 147, 145) == [(105, 109), (145, 147)]
    assert G.plan(_block(90, 30, 0.0), 100, 147, 200) == [(120, 147)]  # the cache ends at 119
    assert G.chunks([(1, 50), (60, 61)], 24) == [(1, 24), (25, 48), (49, 50), (60, 61)]


def test_series_file_roundtrip(tmp_path):
    b = _raw_fixture_hours(with_quality=True)
    p = G.save_series(tmp_path / "s.npz", b, {"id": "x:y"})
    b2, meta = G.load_series(p)
    assert meta["id"] == "x:y" and b2.first == b.first
    np.testing.assert_array_equal(b2.mm, b.mm)
    np.testing.assert_array_equal(b2.flags, b.flags)
    assert G.load_series(tmp_path / "none.npz") == (None, {})


# --- placement ------------------------------------------------------------------------------------
def _cfg(tmp_path: Path, **council) -> object:
    d = tomllib.loads(CONFIG_PATH.read_text())
    d["paths"]["nz_data"] = str(tmp_path)
    d["gauges"] = {"days": 2, "keep_days": 3, "refetch_hours": 3, "chunk_days": 1, "request_gap_s": 0, "retries": 0,
                   "councils": {"test": {"name": "Test Council", "region": "West Coast",
                                         "base_url": ["https://refused.example.test/", "https://hilltop.example.test/"],
                                         "hts": ["missing.hts", "data.hts"], **council}}}
    return parse_config(d)


def test_cell_of_a_gauge(tmp_path):
    cfg = _cfg(tmp_path)
    grid = cfg.grids["ecmwf"]  # centres on x.x0 deg: 171.2 E, 42.5 S is a centre; its cell spans +-0.05
    gs = [G.Gauge("test:a", "test", "a", lon=171.2108, lat=-42.4504), G.Gauge("test:b", "test", "b"),
          G.Gauge("test:c", "test", "c", lon=171.149, lat=-42.551)]
    methods = G.place(gs, grid, cfg)
    a, b, c = gs
    assert (grid.lons[a.cell_col], grid.lats[a.cell_row]) == pytest.approx((171.2, -42.5))
    assert a.cell == a.cell_row * grid.nx + a.cell_col
    assert int(grid.cell_index(a.lon, a.lat)) == a.cell
    assert (grid.lons[c.cell_col], grid.lats[c.cell_row]) == pytest.approx((171.1, -42.6))
    assert b.cell == -1 and not np.isfinite(b.x)
    assert 1_440_000 < a.x < 1_470_000 and 5_290_000 < a.y < 5_310_000  # Greymouth in NZTM
    assert methods == {"council": 3} and a.region == "West Coast" and a.region_id == "west-coast"


def test_region_from_weights_cell_majority(tmp_path):
    import scipy.sparse as sp

    from river_finder.rainfall.weights import CatchmentWeights

    cfg = _cfg(tmp_path)
    grid = cfg.grids["ecmwf"]
    cell = int(grid.cell_index(171.56, -42.83))
    # three catchments in the cell: Canterbury 15 px, West Coast 10 + 10 px -> West Coast
    counts = sp.csr_matrix((np.array([15, 10, 10]), (np.array([0, 1, 2]), np.array([cell] * 3))),
                           shape=(3, grid.size))
    w = CatchmentWeights(grid, counts, np.arange(3), np.array([15, 10, 10]), 64.0, np.full(3, -1),
                         np.array([0, 1, 1], np.int16), ("Canterbury", "West Coast"), np.ones(3, np.float32),
                         np.ones(3, np.float32))
    w.save(cfg.weights_path("ecmwf"))
    gs = [G.Gauge("test:a", "test", "a", lon=171.56, lat=-42.83), G.Gauge("test:b", "test", "b", lon=172.6,
                                                                           lat=-43.5)]
    assert G.place(gs, grid, cfg) == {"cell": 1, "council": 1}
    assert gs[0].region == "West Coast" and gs[0].region_method == "cell"
    assert gs[1].region == "West Coast" and gs[1].region_method == "council"  # no catchment in its cell


def test_region_from_polygons(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    cfg = _cfg(tmp_path)
    # NZTM boxes: West Coast west of x = 1.5e6, Canterbury east of it; a far-away Horizons for the Wanganui split
    c = gpd.GeoDataFrame({"regc": ["12", "13", "08"], "council": ["West Coast", "Canterbury", "Manawatu-Whanganui"]},
                         geometry=[box(1.40e6, 5.10e6, 1.50e6, 5.40e6), box(1.50e6, 5.10e6, 1.60e6, 5.40e6),
                                   box(1.80e6, 5.55e6, 1.81e6, 5.56e6)], crs="EPSG:2193")
    c.to_file(tmp_path / "councils.gpkg", layer="councils", driver="GPKG")
    lc = gpd.GeoDataFrame({"RivName": ["Manawatu River"]}, geometry=[box(1.80e6, 5.55e6, 1.805e6, 5.555e6)],
                          crs="EPSG:2193")
    lc.to_file(tmp_path / "large_catchments.gpkg", layer="large_catchments", driver="GPKG")
    gs = [G.Gauge("test:a", "test", "a", lon=171.2108, lat=-42.4504),  # x ~ 1.457e6
          G.Gauge("test:b", "test", "b", lon=172.63, lat=-43.53),      # Christchurch, x ~ 1.57e6
          G.Gauge("test:c", "test", "c")]
    assert G.place(gs, cfg.grids["ecmwf"], cfg) == {"polygon": 2, "council": 1}
    assert [g.region for g in gs] == ["West Coast", "Canterbury", "West Coast"]


# --- a run against a fake Hilltop server ----------------------------------------------------------
NZST = H.NZST


class FakeHilltop(Http):
    """A Hilltop server for ``hilltop.example.test/data.hts`` with times in NZST; other files answer <Error>, the
    host ``refused.example.test`` is refused like the agent proxy does. ``sites``: name -> dict(lat, lon, records
    = [(NZST naive datetime, mm)], gap_after = set of record times followed by a gap, first, quality = [(t, code)])."""

    def __init__(self, sites: dict):
        super().__init__(retries=0, backoff=0.0, sleep=lambda s: None)
        self.sites, self.calls = sites, []

    def _once(self, url, headers):
        self.calls.append(url)
        u = urllib.parse.urlsplit(url)
        if u.netloc == "refused.example.test":
            raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path != "/data.hts":
            return b"<HilltopServer><Error>Unknown file</Error></HilltopServer>", {}
        req = q["Request"]
        if req == "MeasurementList" and "Site" not in q:
            body = '<HilltopServer><Measurement Name="Flow"/><Measurement Name="Rainfall"/></HilltopServer>'
        elif req == "SiteList":
            parts = []
            for name, s in self.sites.items():
                loc = (f"<Latitude>{s['lat']}</Latitude><Longitude>{s['lon']}</Longitude>" if s.get("lat") else "")
                parts.append(f"<Site Name={quoteattr(name)}>{loc}</Site>")
            body = "<HilltopServer>" + "".join(parts) + "</HilltopServer>"
        elif req == "MeasurementList":
            s = self.sites[q["Site"]]
            body = (f'<HilltopServer><DataSource Name="Rainfall"><TSType>StdSeries</TSType><DataType>SimpleTimeSeries'
                    f'</DataType><Interpolation>Incremental</Interpolation><From>{s["first"].isoformat()}</From>'
                    f'<To>{s["records"][-1][0].isoformat()}</To><Measurement Name="Rainfall"><Units>mm</Units>'
                    f'</Measurement></DataSource></HilltopServer>')
        elif req == "GetData":
            s = self.sites[q["Site"]]
            a, b = datetime.fromisoformat(q["From"]), datetime.fromisoformat(q["To"])
            if q.get("tsType") == "StdQualSeries":
                rows = "".join(f"<E><T>{t.isoformat()}</T><I1>{c}</I1></E>" for t, c in s.get("quality", [])
                               if a <= t <= b)
            else:
                out = []
                for t, v in s["records"]:
                    if a <= t <= b:
                        out.append(f"<E><T>{t.isoformat()}</T><I1>{v}</I1></E>")
                        if t in s.get("gap_after", ()):
                            out.append("<Gap/>")
                rows = "".join(out)
            body = (f'<Hilltop><Measurement SiteName={quoteattr(q["Site"])}><DataSource Name="Rainfall" NumItems="1">'
                    f'<TSType>StdSeries</TSType><ItemInfo ItemNumber="1"><ItemName>Rainfall</ItemName><Units>mm'
                    f'</Units></ItemInfo></DataSource><Data DateFormat="Calendar" NumItems="1">{rows}</Data>'
                    f'</Measurement></Hilltop>')
        else:
            body = f"<HilltopServer><Error>{escape(req)} not served</Error></HilltopServer>"
        return body.encode(), {}

    def getdata(self) -> list[dict]:
        return [{k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(c).query).items()}
                for c in self.calls if "Request=GetData" in c and "tsType" not in c]


def _tips(start: datetime, end: datetime, skip=None) -> list[tuple[datetime, float]]:
    out, t = [], start
    while t <= end:
        if not (skip and skip[0] < t < skip[1]):
            out.append((t, 0.5))
        t += timedelta(minutes=20)
    return out


def _sites(until: datetime) -> dict:
    gap = (datetime(2026, 11, 20, 8), datetime(2026, 11, 20, 10))  # NZST
    return {
        "Alpha at Test Bridge": {"lat": -42.4504, "lon": 171.2108, "first": datetime(2026, 11, 1),
                                 "records": _tips(datetime(2026, 11, 19), until, gap), "gap_after": {gap[0]},
                                 "quality": [(datetime(2026, 11, 20, 0), 600)]},
        "Beta Unlocated": {"first": datetime(2026, 11, 20, 6), "records": _tips(datetime(2026, 11, 20, 6), until)},
    }


def test_run_against_fake_server(tmp_path):
    cfg = _cfg(tmp_path)
    now = utc(2026, 11, 21, 3)                                   # = 2026-11-21 15:00 NZST
    fake = FakeHilltop(_sites(datetime(2026, 11, 21, 15)))
    rep = G.run_gauges(cfg, now=now, http=fake, log=lambda m: None)
    assert rep["answered"] and rep["councils"]["test"]["status"] == "ok"
    assert rep["councils"]["test"]["hts"] == "data.hts" and rep["councils"]["test"]["measurement"] == "Rainfall"
    assert any("refused" in w for w in rep["councils"]["test"]["warnings"])  # https root refused, next one used
    gdir = cfg.gauges_dir
    sites = json.loads((gdir / "sites.json").read_text())
    assert [g["id"] for g in sites["gauges"]] == ["test:Alpha at Test Bridge", "test:Beta Unlocated"]
    gh = G.GaugeHours.load(gdir / "gauges_hourly.npz")
    assert gh.hour[0] == hr(2026, 11, 19, 4) and gh.hour[-1] == hr(2026, 11, 21, 3)
    assert gh.id.tolist() == ["test:Alpha at Test Bridge", "test:Beta Unlocated"]
    assert gh.regions == ("West Coast",) and gh.region_of.tolist() == [0, 0]
    assert gh.cell[0] >= 0 and gh.cell[1] == -1
    a, b = 0, 1
    col = {int(h): i for i, h in enumerate(gh.hour)}
    # Alpha: three tips an hour; QC 600 up to 2026-11-20 00:00 NZST = 11-19 12Z; the gap 08-10 NZST = 20-22Z
    assert gh.mm[a, col[hr(2026, 11, 19, 12)]] == 1.5 and gh.state[a, col[hr(2026, 11, 19, 12)]] == G.GOOD
    assert gh.state[a, col[hr(2026, 11, 19, 13)]] == G.RAW
    assert [G.STATES[gh.state[a, col[hr(2026, 11, 19, h)]]] for h in (20, 21, 22, 23)] == ["raw", "missing",
                                                                                         "missing", "raw"]
    assert gh.mm[a, col[hr(2026, 11, 21, 3)]] == 1.5
    ok = gh.ok[a]
    assert ok.sum() == len(gh.hour) - 2 and np.nansum(gh.mm[a][ok]) == 1.5 * ok.sum()
    # Beta: its record starts 2026-11-20 06:00 NZST = 11-19 18Z: hours up to then are outside
    assert gh.state[b, col[hr(2026, 11, 19, 18)]] == G.MISSING and gh.flags[b, col[hr(2026, 11, 19, 18)]] == G.F_OUTSIDE
    assert gh.mm[b, col[hr(2026, 11, 19, 19)]] == 1.5
    w = gh.window(hours=24)
    assert w["mm"].tolist() == [36.0, 36.0] and w["n_ok"].tolist() == [24, 24]
    rr = gh.region_rows()
    assert list(rr) == ["West Coast"] and rr["West Coast"].tolist() == [0, 1]
    rj = json.loads((gdir / "gauges_hourly.json").read_text())
    assert rj["sites"][0]["mm_24h"] == 36.0 and rj["sites"][0]["region_id"] == "west-coast"
    assert rj["regions"]["West Coast"]["gauges"] == 2 and rj["summary"]["gauges"] == 2
    assert rj["councils"]["test"]["attribution"] == ""
    # requests: 2 days in 1-day chunks, NZST times, an hour of margin before each chunk
    gd = fake.getdata()
    assert [x["From"] for x in gd if x["Site"] == "Alpha at Test Bridge"] == ["2026-11-19T14:00:00",
                                                                                "2026-11-20T14:00:00"]
    assert all(x["ShowQuality"] == "Yes" for x in gd)

    # an hour later: only the newest refetch_hours (3) and the new hour are asked for
    fake2 = FakeHilltop(_sites(datetime(2026, 11, 21, 16)))
    G.run_gauges(cfg, now=utc(2026, 11, 21, 4), http=fake2, log=lambda m: None)
    gd = fake2.getdata()
    assert [(x["From"], x["To"]) for x in gd if x["Site"] == "Alpha at Test Bridge"] == [
        ("2026-11-21T12:00:00", "2026-11-21T17:00:00")]  # hours ending 02Z..04Z, minus / plus the margin
    gh2 = G.GaugeHours.load(gdir / "gauges_hourly.npz")
    assert gh2.hour[0] == hr(2026, 11, 19, 4) and gh2.hour[-1] == hr(2026, 11, 21, 4)
    assert gh2.mm[0, -1] == 1.5
    np.testing.assert_array_equal(gh2.state[:, :-1], gh.state)  # the cached hours are kept as they were


def test_run_when_nothing_answers(tmp_path):
    cfg = _cfg(tmp_path, base_url="https://refused.example.test/")
    rep = G.run_gauges(cfg, now=utc(2026, 11, 21, 3), http=FakeHilltop({}), log=lambda m: None)
    assert not rep["answered"] and rep["councils"]["test"]["status"].startswith("failed")
    assert "refused by the proxy" in rep["councils"]["test"]["status"]
    assert "gauges" in G.status_line(cfg)


def test_sites_only_and_discover(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeHilltop(_sites(datetime(2026, 11, 21, 15)))
    rep = G.run_gauges(cfg, now=utc(2026, 11, 21, 3), sites_only=True, http=fake, log=lambda m: None)
    assert rep["councils"]["test"]["status"] == "sites only" and not fake.getdata()
    assert not (cfg.gauges_dir / "gauges_hourly.npz").exists()
    assert len(json.loads((cfg.gauges_dir / "sites.json").read_text())["gauges"]) == 2
    out = G.discover(cfg, http=FakeHilltop(_sites(datetime(2026, 11, 21, 15))), now=utc(2026, 11, 21, 3),
                     log=lambda m: None)
    probes = out["councils"]["test"]["probes"]
    assert [(p["base_url"], p["hts"]) for p in probes] == [("https://refused.example.test/", "missing.hts"),
                                                            ("https://refused.example.test/", "data.hts"),
                                                            ("https://hilltop.example.test/", "missing.hts"),
                                                            ("https://hilltop.example.test/", "data.hts")]
    last = probes[-1]
    assert last["chosen"] == "Rainfall" and last["sites"] == 2 and last["sites_located"] == 1
    assert last["sample"]["last_record_minus_now_min_as_NZST"] == 0.0
    assert last["sample"]["last_record_minus_now_min_as_Pacific/Auckland"] == -60.0
    assert (cfg.gauges_dir / "discover.json").exists()


# --- config, HTTP ---------------------------------------------------------------------------------
def test_repo_config_councils():
    from river_finder.national import FILTER_ORDER

    g = load_config().gauges
    assert {c.id for c in g.councils if c.enabled} == {"wcrc", "ecan", "tdc"}
    assert not g.council("ecan_hilltop").enabled  # ECan's Hilltop server: kept, off (resets every connection)
    assert g.council("ecan").kind == "ecan-portal" and not g.council("ecan").hts
    for c in g.councils:
        assert c.region in FILTER_ORDER and c.timezone == "NZST"  # checked live for all three (2026-11-21)
        assert all(u.endswith("/") for u in c.base_urls) and all(h.endswith(".hts") for h in c.hts)
        assert c.kind == "ecan-portal" or c.hts
    assert "Rainfall (raw)" in g.council("wcrc").measurements
    assert g.days <= g.keep_days and g.inactive_days < g.days


@pytest.mark.parametrize("patch,match", [
    ({"timezone": "NZDT"}, "timezone"),
    ({"method": "sum"}, "method"),
    ({"base_url": "https://x.example/data.hts"}, "base_url"),
    ({"hts": ["data"]}, "hts"),
    ({"bogus": 1}, "unknown key"),
])
def test_council_config_errors(tmp_path, patch, match):
    with pytest.raises(ValueError, match=match):
        _cfg(tmp_path, **patch)


def test_gauges_config_errors():
    d = tomllib.loads(CONFIG_PATH.read_text())
    d["gauges"]["days"] = 90
    with pytest.raises(ValueError, match="keep_days"):
        parse_config(d)


def test_proxy_refusal_is_not_retried():
    class Refusing(Http):
        def __init__(self):
            super().__init__(retries=4, backoff=0.0, sleep=lambda s: None)
            self.n = 0

        def _once(self, url, headers):
            self.n += 1
            raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    h = Refusing()
    with pytest.raises(FetchError, match="refused by the proxy"):
        h.get("https://hilltop.example.test/data.hts")
    assert h.n == 1 and h.stats.retries == 0


def test_pacer_spaces_requests():
    t = [0.0]
    slept = []
    p = H.Pacer(1.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    p.wait()
    t[0] += 0.25
    p.wait()
    t[0] += 3.0
    p.wait()
    assert slept == [pytest.approx(0.75)]


def test_closed_site_costs_no_data_requests(tmp_path):
    cfg = _cfg(tmp_path)
    closed = {"Gamma Closed": {"lat": -42.0, "lon": 172.0, "first": datetime(2001, 1, 1),
                               "records": _tips(datetime(2026, 9, 30), datetime(2026, 10, 1))}}
    # and one whose record ended 20 days ago: still active (inactive_days = 30), but outside the 2 days asked
    closed["Delta Quiet"] = {"lat": -42.1, "lon": 172.1, "first": datetime(2001, 1, 1),
                             "records": _tips(datetime(2026, 11, 1), datetime(2026, 11, 1, 12))}
    fake = FakeHilltop(closed)
    rep = G.run_gauges(cfg, now=utc(2026, 11, 21, 3), http=fake, log=lambda m: None)
    assert not [c for c in fake.calls if "Request=GetData" in c]  # neither data nor quality series
    hours = G.GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    assert hours.id.tolist() == ["test:Delta Quiet"]  # the closed site is left out of the hourly output
    assert set(hours.state[0].tolist()) == {G.MISSING} and set(hours.flags[0].tolist()) == {G.F_OUTSIDE}
    assert len(hours.hour) == 48
    c = rep["councils"]["test"]
    assert (c["sites"], c["sites_active"], c["sites_skipped_inactive"], c["gauges"]) == (2, 1, 0, 1)
    sites = {g["name"]: g for g in json.loads((cfg.gauges_dir / "sites.json").read_text())["gauges"]}
    assert not sites["Gamma Closed"]["active"] and sites["Gamma Closed"]["checked"] == "2026-11-21T03:00:00Z"
    assert sites["Gamma Closed"]["record_last"] == "2026-09-30T12:00:00Z"  # 2026-10-01 00:00 NZST
    # the next runs send it no request at all, until recheck_days (7) after its check
    for day, asked in ((22, False), (27, False), (29, True)):
        fake = FakeHilltop(closed)
        rep = G.run_gauges(cfg, now=utc(2026, 11, day, 3), http=fake, log=lambda m: None)
        assert any("Gamma" in urllib.parse.unquote(u) for u in fake.calls) == asked, day
        assert rep["councils"]["test"]["sites_skipped_inactive"] == (0 if asked else 1)
        sites = {g["name"]: g for g in json.loads((cfg.gauges_dir / "sites.json").read_text())["gauges"]}
        assert sites["Gamma Closed"]["record_last"] == "2026-09-30T12:00:00Z" and not sites["Gamma Closed"]["active"]
    assert sites["Gamma Closed"]["checked"] == "2026-11-29T03:00:00Z"


# --- the rain series of a site (real WCRC and TDC answers) ------------------------------------------
WCRC_HINTS = ("Rainfall", "Rainfall (corrected)", "Rainfall (raw)")
NOW_LIVE = utc(2026, 11, 21, 21, 7).timestamp()  # when the real fixtures were fetched


def test_matching_measurements_and_raw_names():
    names = ["Stage", "Rainfall", "Check diff rain", "1 Day cumulative rainfall", "Rainfall (raw)", "Rainfall CR10x",
             "Rainfall (check gauge)", "Rainfall (corrected)", "Rainfall [Rainfall DS4483]", "Item1C [Rainfall (raw)]"]
    assert H.matching_measurements(names, WCRC_HINTS) == ["Rainfall", "Rainfall [Rainfall DS4483]",
                                                          "Rainfall (corrected)", "Rainfall (raw)"]
    assert H.matching_measurements(["Flow"], WCRC_HINTS) == []
    assert H.is_raw_name("Rainfall (raw)") and H.is_raw_name("Raw Rainfall") and H.is_raw_name("RAW")
    assert not H.is_raw_name("Rainfall") and not H.is_raw_name("Drawdown")


def test_wcrc_site_takes_the_live_raw_series():
    """Arthurs Pass EWS: "Rainfall" (verified) ends 2023-10-16, "Rainfall (raw)" reaches 2026-11-22 07:00 NZST."""
    srcs = H.parse_site_measurements(fx("wcrc_site_measurements.xml"))
    std = [(s.datasource, s.measurement) for k, s in H.rain_sources(srcs, WCRC_HINTS)]
    assert std == [("Rainfall", "Rainfall"), ("Rainfall (raw)", "Rainfall (raw)")]  # no VMs, no quality series
    src = H.choose_source(srcs, WCRC_HINTS, "NZST", NOW_LIVE)
    assert (src.datasource, src.request_name, src.tstype, src.units) == ("Rainfall (raw)", "Rainfall (raw)",
                                                                        "StdSeries", "mm")
    assert src.last == datetime(2026, 11, 22, 7) and H.is_raw_name(src.datasource)
    # in 2023 the verified series reached nearest to now: it wins
    old = utc(2023, 10, 16, 0).timestamp()
    assert H.choose_source(srcs, WCRC_HINTS, "NZST", old).datasource == "Rainfall"
    assert H.choose_source(srcs, ("Flow",), "NZST", NOW_LIVE) is None


def test_tdc_site_takes_rainfall_not_raw_or_forecast_statistics():
    """HY Takaka at Harwoods: "Rainfall" is live, "Raw Rainfall" ends at the last visit, "Ensemble Rainfall
    Statistics" runs into 2027 (not a hint)."""
    srcs = H.parse_site_measurements(fx("tdc_site_measurements.xml"))
    src = H.choose_source(srcs, ("Rainfall", "Raw Rainfall"), "NZST", NOW_LIVE)
    assert (src.datasource, src.request_name) == ("Rainfall", "Rainfall") and src.last == datetime(2026, 11, 22, 9)
    assert H.choose_source(srcs, ("Raw Rainfall",), "NZST", NOW_LIVE).datasource == "Raw Rainfall"
    assert H.choose_source(srcs, ("Precipitation Month Mean", "Rainfall"), "NZST", NOW_LIVE).datasource == "Rainfall"


def test_choose_source_ties_go_to_the_earlier_hint():
    def src(name, last):
        return H.DataSourceInfo(name, name, name, "mm", "StdSeries", last=last)

    now = utc(2026, 11, 21, 21).timestamp()  # 22/11 09:00 NZST
    both = [src("Rainfall (raw)", datetime(2026, 11, 22, 8, 55)), src("Rainfall", datetime(2026, 11, 22, 6))]
    assert H.choose_source(both, WCRC_HINTS, "NZST", now).datasource == "Rainfall"  # 3 h apart: verified first
    both[1].last = datetime(2026, 11, 21, 20)
    assert H.choose_source(both, WCRC_HINTS, "NZST", now).datasource == "Rainfall (raw)"  # 13 h behind: raw
    assert H.choose_source([src("Rainfall", None)], WCRC_HINTS, "NZST", now).datasource == "Rainfall"


def test_real_wcrc_getdata_and_request_encoding():
    s = H.parse_data(fx("wcrc_getdata_raw.xml"))
    assert s.site == "Arthurs Pass EWS" and s.measurement == "Rainfall (raw)" and s.item == "Rainfall (raw)"
    assert len(s) == 8 and s.times[0] == datetime(2026, 11, 22) and s.times[-1] == datetime(2026, 11, 22, 7)
    assert s.values.tolist() == [0.0] * 8 and s.quality.tolist() == [200] * 8 and s.interpolation == "Incremental"
    # spaces must go as %20: WCRC reads "+" literally ("No data source for Rainfall+(raw).")
    u = H.build_url("https://hilltop.wcrc.govt.nz/", "data.hts", "GetData", Site="Ahaura Rv @ Gorge WCRC",
                    Measurement="Rainfall (raw)")
    assert "+" not in u and u.endswith("Site=Ahaura%20Rv%20%40%20Gorge%20WCRC&Measurement=Rainfall%20%28raw%29")


class FakeTwoSeries(Http):
    """A WCRC-like server: "Rainfall" (verified, ended 2023) lists only Alpha, "Rainfall (raw)" (live, QC 600 on
    purpose) lists Alpha and Beta; GetData of "Rainfall" has nothing in the hours asked."""

    def __init__(self):
        super().__init__(retries=0, backoff=0.0, sleep=lambda s: None)
        self.calls = []

    def _once(self, url, headers):
        self.calls.append(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        req = q["Request"]
        site = '<Site Name="{}"><Latitude>-42.4</Latitude><Longitude>171.7</Longitude></Site>'
        if req == "MeasurementList" and "Site" not in q:
            body = ('<HilltopServer><Measurement Name="Flow"/><Measurement Name="Rainfall"/>'
                    '<Measurement Name="Rainfall (raw)"/></HilltopServer>')
        elif req == "SiteList":
            names = ["Alpha"] if q["Measurement"] == "Rainfall" else ["Alpha", "Beta"]
            body = "<HilltopServer>" + "".join(site.format(n) for n in names) + "</HilltopServer>"
        elif req == "MeasurementList":
            ds = ('<DataSource Name="{0}"><TSType>StdSeries</TSType><Interpolation>Incremental</Interpolation>'
                  '<From>2021-09-28T00:00:00</From><To>{1}</To><Measurement Name="{0}"><RequestAs>{0}</RequestAs>'
                  '<Units>mm</Units></Measurement></DataSource>')
            body = ("<HilltopServer>" + (ds.format("Rainfall", "2023-10-16T05:00:00") if q["Site"] == "Alpha" else "")
                    + ds.format("Rainfall (raw)", "2026-11-21T15:00:00") + "</HilltopServer>")
        elif req == "GetData" and q.get("tsType") == "StdQualSeries":
            body = f'<Hilltop><Measurement SiteName="{q["Site"]}"><Data></Data></Measurement></Hilltop>'
        elif req == "GetData" and q["Measurement"] == "Rainfall (raw)":
            a, b = datetime.fromisoformat(q["From"]), datetime.fromisoformat(q["To"])
            t, rows = a.replace(minute=0, second=0), []
            while t <= min(b, datetime(2026, 11, 21, 15)):
                rows.append(f"<E><T>{t.isoformat()}</T><I1>1.0</I1><Q1>600</Q1></E>")
                t += timedelta(hours=1)
            body = (f'<Hilltop><Measurement SiteName="{q["Site"]}"><DataSource Name="Rainfall (raw)"><ItemInfo '
                    f'ItemNumber="1"><ItemName>Rainfall (raw)</ItemName><Units>mm</Units></ItemInfo></DataSource>'
                    f'<Data>{"".join(rows)}</Data></Measurement></Hilltop>')
        else:
            body = f"<HilltopServer><Error>No data for {escape(q.get('Measurement', ''))}</Error></HilltopServer>"
        return body.encode(), {}


def test_run_uses_each_sites_live_series(tmp_path):
    cfg = _cfg(tmp_path, base_url=["https://hilltop.example.test/"], hts=["data.hts"], measurements=list(WCRC_HINTS))
    # a cache of the wrong series from an earlier run: all hours "outside" (fetched), as WCRC's first live run left
    g0 = G.Gauge("test:Alpha", "test", "Alpha")
    G.save_series(G.series_path(cfg.gauges_dir, g0), G.HourBlock.empty(hr(2026, 11, 19, 4), 48, G.F_OUTSIDE),
                  {"measurement": "Rainfall"})
    fake = FakeTwoSeries()
    rep = G.run_gauges(cfg, now=utc(2026, 11, 21, 3), http=fake, log=lambda m: None)
    c = rep["councils"]["test"]
    assert c["measurement"] == "Rainfall, Rainfall (raw)" and (c["sites"], c["sites_active"]) == (2, 2)
    assert any("series changed" in w for w in c["warnings"])
    sites = {g["name"]: g for g in json.loads((cfg.gauges_dir / "sites.json").read_text())["gauges"]}
    for n in ("Alpha", "Beta"):
        assert (sites[n]["measurement"], sites[n]["datasource"], sites[n]["raw"]) == ("Rainfall (raw)",
                                                                                     "Rainfall (raw)", True)
        assert sites[n]["record_last"] == "2026-11-21T03:00:00Z"
    gd = [u for u in fake.calls if "Request=GetData" in u and "tsType" not in u]
    assert gd and all("Measurement=Rainfall%20%28raw%29" in u for u in gd)
    gh = G.GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    assert gh.id.tolist() == ["test:Alpha", "test:Beta"]
    assert np.all(gh.mm == 1.0) and set(gh.state.ravel().tolist()) == {G.RAW}  # QC 600, but raw telemetry
    assert c["usable_frac"] == 1.0 and c["gauges"] == 2


# --- ECan Open Data Portal (real answers) -----------------------------------------------------------
def test_portal_site_list():
    sites = E.parse_sites((ECAN / "sites_all.csv").read_bytes())
    by = {s.name: s for s in sites}
    assert list(by) == ["Shingle Fan", "Arthur's Pass EWS (ESNZ)", "Esk", "Botanic Gardens (CCC)", "Tai Tapu Yard"]
    s = by["Shingle Fan"]
    assert (s.site_no, s.owner, s.area, s.last_sample) == ("231910", "ECan", "N", datetime(2026, 11, 22, 9))
    assert (s.lon, s.lat) == pytest.approx((173.90133667, -42.10688019))
    assert by["Esk"].last_sample == datetime(2026, 11, 19, 20)  # 8:00:00 PM
    assert by["Arthur's Pass EWS (ESNZ)"].owner == "ESNZ" and by["Botanic Gardens (CCC)"].owner == "CCC"
    assert not by["Tai Tapu Yard"].located and by["Tai Tapu Yard"].last_sample == datetime(2026, 11, 22, 8, 45)
    assert E.owner_name("Ecan") == E.owner_name("ECAN") == "ECan"
    with pytest.raises(E.PortalError, match="not CSV"):
        E.parse_sites(b"<!DOCTYPE html><html>Sorry, an error occurred</html>")
    with pytest.raises(E.PortalError, match="without column"):
        E.parse_sites(b"site_no,DateTime,RainfallTotal\r\n")


def test_portal_hourly_totals_to_hours():
    """Shingle Fan, Period=2_Days: 47 hour-end stamps (xx:59:59 or xx:00:00), 17.5 mm on 21/11 as the site list's
    "-1 Day"; a stamp of 09:00 NZST on 22/11 is the hour ending 2026-11-21 21:00Z."""
    tab = E.parse_hourly((ECAN / "hourly_2days_231910.csv").read_bytes())
    assert len(tab.times) == 47 and tab.sites == ("231910",) and tab.off_hour == 0
    assert tab.times[0] == datetime(2026, 11, 20, 11) and tab.times[1] == datetime(2026, 11, 20, 12)  # 10:59:59 AM
    assert tab.times[-1] == datetime(2026, 11, 22, 9)
    assert all(b - a == timedelta(hours=1) for a, b in zip(tab.times, tab.times[1:], strict=False))
    day = [v for t, v in zip(tab.times, tab.mm, strict=True) if t.date() == datetime(2026, 11, 21).date()]
    assert sum(day) == 17.5
    t_s, _ = H.to_utc(tab.times, "NZST")
    first, last = hr(2026, 11, 19, 23), hr(2026, 11, 21, 22)
    b = G.hours_from_records(t_s, tab.mm, first, last, rec_end=utc(2026, 11, 21, 21).timestamp(), method="total-end")
    assert b.hours[0] == hr(2026, 11, 19, 23) and np.isfinite(b.mm[:-1]).all()
    assert G.STATES[b.state[-1]] == "missing" and G.F_OUTSIDE & b.flags[-1]  # after the last sample
    assert float(np.nansum(b.mm)) == float(tab.mm.sum())
    assert E.round_hour(datetime(2026, 11, 8, 9, 59, 59, 943000)) == datetime(2026, 11, 8, 10)
    assert E.round_hour(datetime(2026, 11, 8, 10, 0, 0, 20000)) == datetime(2026, 11, 8, 10)
    assert E.parse_time("8/11/2026 9:59:59 AM") == datetime(2026, 11, 8, 9, 59, 59)  # no leading zeros
    assert E.parse_time("2026-11-22T09:00:00+13:00") == datetime(2026, 11, 22, 9)  # JSON's offset dropped
    assert E.period_for(10) == ("2_Days", 45) and E.period_for(48) == ("1_Week", 165)
    assert E.period_for(800) == ("1_Month", 669)


class FakePortal(Http):
    """ECan's portal at portal.example.test: the real site list (Esk's last sample moved back to 19/10) and, per
    site, ``Period``'s rows back from the site's last whole hour, 0.5 mm each; as at ECan's event recorders, hours
    without a record have no row: Tai Tapu Yard has one every 3 hours, Arthur's Pass none (dry)."""

    ROWS = {"2_Days": 47, "1_Week": 167, "2_Weeks": 334, "1_Month": 742}
    EVERY = {"326510": 3}
    DRY = {"219506"}

    def __init__(self):
        super().__init__(retries=0, backoff=0.0, sleep=lambda s: None)
        self.sites = (ECAN / "sites_all.csv").read_bytes().replace(b"19/11/2026 8:00:00 PM", b"19/10/2026 8:00:00 PM")
        self.last = {s.site_no: s.last_sample for s in E.parse_sites(self.sites)}
        self.calls = []

    @staticmethod
    def stamp(t: datetime) -> str:
        t = t - timedelta(seconds=1) if t.hour % 2 else t  # the portal's xx:59:59 jitter
        return f"{t.day}/{t.month:02d}/{t.year} {t.hour % 12 or 12}:{t:%M:%S} {'AM' if t.hour < 12 else 'PM'}"

    def _once(self, url, headers):
        self.calls.append(url)
        u = urllib.parse.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.startswith("/data/51/") and q.get("Sites") == "All":
            return self.sites, {}
        if u.path.startswith("/data/78/") and q.get("Period") in self.ROWS:
            end = self.last[q["SiteNo"]].replace(minute=0, second=0)
            rows = [f"{q['SiteNo']},{self.stamp(end - timedelta(hours=k))},0.5"
                    for k in range(self.ROWS[q["Period"]] - 1, -1, -1)
                    if k % self.EVERY.get(q["SiteNo"], 1) == 0 and q["SiteNo"] not in self.DRY]
            return ("site_no,DateTime,RainfallTotal\r\n" + "\r\n".join(rows) + "\r\n").encode(), {}
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    def periods(self) -> dict[str, str]:
        out = {}
        for c in self.calls:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(c).query).items()}
            if "SiteNo" in q:
                out[q["SiteNo"]] = q["Period"]
        return out


def _portal_cfg(tmp_path: Path):
    d = tomllib.loads(CONFIG_PATH.read_text())
    d["paths"]["nz_data"] = str(tmp_path)
    d["gauges"] = {"days": 2, "keep_days": 3, "refetch_hours": 3, "request_gap_s": 0, "retries": 0,
                   "councils": {"ec": {"name": "Test Canterbury", "kind": "ecan-portal", "region": "Canterbury",
                                       "base_url": "https://portal.example.test/", "attribution": "ECan"}}}
    return parse_config(d)


def test_run_against_fake_portal(tmp_path):
    cfg = _portal_cfg(tmp_path)
    assert cfg.gauges.council("ec").hts == ()
    now = utc(2026, 11, 21, 21)                                # = 22/11 09:00 NZST
    fake = FakePortal()
    rep = G.run_gauges(cfg, now=now, http=fake, log=lambda m: None)
    c = rep["councils"]["ec"]
    assert rep["answered"] and c["status"] == "ok" and c["kind"] == "ecan-portal"
    assert (c["sites"], c["sites_active"], c["sites_skipped_inactive"], c["gauges"]) == (5, 4, 1, 4)
    assert c["owners"] == {"ECan": 3, "ESNZ": 1, "CCC": 1}
    # one request for the list, one per active site, the shortest period surely reaching 47-48 hours back from the
    # site's last whole hour (2_Days: 45 assumed)
    assert len(fake.calls) == 5 and set(fake.periods().values()) == {"1_Week"}
    assert set(fake.periods()) == {"231910", "219506", "325616", "326510"}
    gh = G.GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    names = gh.name.tolist()
    assert names == ["Shingle Fan", "Arthur's Pass EWS (ESNZ)", "Botanic Gardens (CCC)", "Tai Tapu Yard"]
    assert gh.hour[-1] == hr(2026, 11, 21, 21) and len(gh.hour) == 48
    assert gh.cell[names.index("Tai Tapu Yard")] == -1 and gh.cell[0] >= 0
    assert G.GOOD not in gh.state  # provisional values: raw at best
    assert set(gh.state[0].tolist()) == {G.RAW} and np.all(gh.mm[0] == 0.5)
    # Arthur's Pass: no rows at all (dry): 0 mm up to its last sample (08:00 NZST = 20Z), the hour ending 21Z after it
    ap = names.index("Arthur's Pass EWS (ESNZ)")
    assert np.all(gh.mm[ap, :-1] == 0.0) and np.all(gh.state[ap, :-1] == G.RAW)
    assert G.STATES[gh.state[ap, -1]] == "missing" and gh.flags[ap, -1] & G.F_OUTSIDE
    # Tai Tapu Yard: a row every 3 hours, 0 mm in the hours between
    tt = names.index("Tai Tapu Yard")
    assert np.nansum(gh.mm[tt]) == 0.5 * 16 and np.all(gh.ok[tt, :-1])
    # Botanic Gardens: last sample 08:55 NZST: the hour ending 09:00 NZST has no total yet (partly after its record)
    bg = names.index("Botanic Gardens (CCC)")
    assert G.STATES[gh.state[bg, -1]] == "suspect" and gh.flags[bg, -1] & G.F_PARTIAL and np.all(gh.ok[bg, :-1])
    sites = {g["name"]: g for g in json.loads((cfg.gauges_dir / "sites.json").read_text())["gauges"]}
    assert sites["Esk"]["active"] is False and sites["Esk"]["site_no"] == "311910"
    assert sites["Botanic Gardens (CCC)"]["record_last"] == "2026-11-21T20:55:00Z" and sites["Shingle Fan"]["raw"]
    assert rep["region_ids"] == {"Canterbury": "canterbury"}

    # an hour later only the refetch hours are needed: the shortest period
    fake2 = FakePortal()
    G.run_gauges(cfg, now=utc(2026, 11, 21, 22), http=fake2, log=lambda m: None)
    assert set(fake2.periods().values()) == {"2_Days"}


def test_portal_first_run_takes_the_whole_month(tmp_path):
    """35 days asked, a month (742 rows) given: every row is used, the older hours stay not_fetched, and the next
    run does not ask for a month again just because they are missing."""
    d = tomllib.loads(CONFIG_PATH.read_text())
    d["paths"]["nz_data"] = str(tmp_path)
    d["gauges"] = {"days": 35, "keep_days": 60, "refetch_hours": 3, "request_gap_s": 0, "retries": 0,
                   "councils": {"ec": {"name": "T", "kind": "ecan-portal", "base_url": "https://portal.example.test/"}}}
    cfg = parse_config(d)
    fake = FakePortal()
    G.run_gauges(cfg, now=utc(2026, 11, 21, 21), http=fake, log=lambda m: None)
    assert set(fake.periods().values()) == {"1_Month"}
    gh = G.GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
    i = gh.name.tolist().index("Shingle Fan")
    assert gh.ok[i].sum() == 742 and np.all(gh.mm[i][gh.ok[i]] == 0.5)
    first_ok = int(np.flatnonzero(gh.ok[i])[0])
    assert gh.hour[first_ok] == hr(2026, 11, 21, 21) - 741 and np.all(gh.ok[i][first_ok:])
    assert np.all(gh.flags[i][:first_ok] & G.F_NOT_FETCHED)  # the axis starts at the earliest hour any gauge holds
    fake2 = FakePortal()
    G.run_gauges(cfg, now=utc(2026, 11, 21, 22), http=fake2, log=lambda m: None)
    assert set(fake2.periods().values()) == {"2_Days"}


def test_portal_site_list_failure_is_unanswered(tmp_path):
    cfg = _portal_cfg(tmp_path)

    class Down(FakePortal):
        def _once(self, url, headers):
            raise urllib.error.URLError("Connection reset by peer")

    rep = G.run_gauges(cfg, now=utc(2026, 11, 21, 21), http=Down(), log=lambda m: None)
    assert not rep["answered"] and rep["councils"]["ec"]["status"].startswith("failed")


def test_portal_council_config():
    d = tomllib.loads(CONFIG_PATH.read_text())
    d["gauges"]["councils"]["x"] = {"name": "X", "kind": "sos", "base_url": "https://x.example/"}
    with pytest.raises(ValueError, match="kind"):
        parse_config(d)
    d["gauges"]["councils"]["x"] = {"name": "X", "base_url": "https://x.example/"}  # hilltop needs hts
    with pytest.raises(ValueError, match="lacks hts"):
        parse_config(d)

"""ECMWF IFS ingestion (rf-t26.3), without network: synthetic GRIB2 / .om files behind a fake HTTP."""

import json
import struct
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import scipy.sparse as sp

from river_finder.rainfall import CatchmentWeights, GridSpec, HourlyBuffer, hour_index, hour_time, ingest
from river_finder.rainfall.config import load_config, parse_config
from river_finder.rainfall.ecmwf import (
    EcmwfSource,
    GribReader,
    OctahedralGrid,
    OpenMeteoReader,
    Reader,
    decode_grib_tp,
    fc_plan,
    find_run,
    obs_plan,
    parse_grib,
    parse_run,
)
from river_finder.rainfall.fetch import Bucket, FetchError, Http, NotFound
from river_finder.rainfall.pipeline import run_ecmwf, status

RUN = datetime(2026, 11, 19, 6, tzinfo=UTC)
NATIVE = GridSpec("t_native", lon0=170.0, lat0=-40.0, dlon=0.1, dlat=-0.1, nx=8, ny=6)   # stands in for 0.1° global
WIN = GridSpec("t_win", lon0=170.2, lat0=-40.1, dlon=0.1, dlat=-0.1, nx=4, ny=3)        # the "NZ window" in it
S3, GCS, OM = "https://fake-s3/", "https://fake-gcs/", "https://fake-om/"


# --- fake HTTP -----------------------------------------------------------------------------------
class FakeHttp(Http):
    """Serves ``objects`` (full URL -> bytes), S3 v1 listings and byte ranges; ``fail`` = URL prefixes that
    always answer 503."""

    def __init__(self, objects: dict[str, bytes], fail=()):
        super().__init__(retries=2, backoff=0.0, sleep=lambda s: None)
        self.objects, self.fail, self.calls = objects, tuple(fail), []

    def _once(self, url, headers):
        self.calls.append((url, headers.get("Range")))
        if url.startswith(self.fail):
            raise urllib.error.HTTPError(url, 503, "SlowDown", {}, None)
        if "?" in url:
            base, q = url.split("?", 1)
            prefix = urllib.parse.parse_qs(q)["prefix"][0]
            keys = sorted(k[len(base):] for k in self.objects if k.startswith(base + prefix))
            xml = ("<ListBucketResult><IsTruncated>false</IsTruncated>"
                   + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys) + "</ListBucketResult>")
            return xml.encode(), {}
        if url not in self.objects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        body, rng = self.objects[url], headers.get("Range")
        if not rng:
            return body, {}
        a, b = rng.split("=")[1].split("-")
        if a == "":
            a, b = max(0, len(body) - int(b)), len(body) - 1
        a, b = int(a), (int(b) if b else len(body) - 1)
        return body[a:b + 1], {"Content-Range": f"bytes {a}-{b}/{len(body)}"}

    def grib_ranges(self):
        return [(u, r) for u, r in self.calls if u.endswith(".grib2")]


# --- synthetic GRIB2 (simple packing) -----------------------------------------------------------
def _sm32(v):
    return struct.pack(">I", (abs(v) | 0x80000000) if v < 0 else v)


def _sm16(v):
    return struct.pack(">H", (abs(v) | 0x8000) if v < 0 else v)


def _sec(num, body):
    return struct.pack(">IB", 5 + len(body), num) + body


def make_tp(values_m, run, step1, grid=NATIVE, step0=0, number=193):
    """GRIB2 tp message, template 4.8 (accumulation over step0..step1 h), 24-bit simple packing."""
    nj, ni = values_m.shape
    u = lambda x: round(x * 1e6)  # noqa: E731
    s1 = struct.pack(">HHBBBHBBBBBBB", 98, 0, 32, 1, 1, run.year, run.month, run.day, run.hour, 0, 0, 0, 1)
    la1, lo1 = grid.lat0, grid.lon0
    la2, lo2 = la1 + (nj - 1) * grid.dlat, lo1 + (ni - 1) * grid.dlon
    s3 = (struct.pack(">BIBBH", 0, ni * nj, 0, 0, 0)
          + struct.pack(">BBIBIBI", 6, 0xFF, 0xFFFFFFFF, 0xFF, 0xFFFFFFFF, 0xFF, 0xFFFFFFFF)
          + struct.pack(">IIII", ni, nj, 0, 0xFFFFFFFF) + _sm32(u(la1)) + _sm32(u(lo1)) + bytes([48])
          + _sm32(u(la2)) + _sm32(u(lo2)) + struct.pack(">II", u(grid.dlon), u(-grid.dlat)) + bytes([0]))
    end = run + timedelta(hours=step1)
    s4 = (struct.pack(">HHBBBBBHBBI", 0, 8, 1, number, 2, 0xFF, 0xA1, 0, 0, 1, step0)
          + struct.pack(">BBIBBI", 1, 0xFF, 0xFFFFFFFF, 0xFF, 0xFF, 0xFFFFFFFF)
          + struct.pack(">HBBBBBBI", end.year, end.month, end.day, end.hour, 0, 0, 1, 0)
          + struct.pack(">BBBIBI", 1, 2, 1, step1 - step0, 0xFF, 0))
    v = np.asarray(values_m, dtype=np.float64).ravel()
    ref, e, nbits = float(v.min()), -24, 24
    x = np.round((v - ref) * 2.0 ** -e).astype(np.int64)
    s5 = struct.pack(">IH", v.size, 0) + struct.pack(">f", ref) + _sm16(e) + _sm16(0) + bytes([nbits, 0])
    bits = ((x[:, None] >> np.arange(nbits - 1, -1, -1)) & 1).astype(np.uint8).ravel()
    body = (_sec(1, s1) + _sec(3, s3) + _sec(4, s4) + _sec(5, s5) + _sec(6, bytes([0xFF]))
            + _sec(7, np.packbits(bits).tobytes()) + b"7777")
    return b"GRIB\0\0" + bytes([0, 2]) + struct.pack(">Q", 16 + len(body)) + body


def grib_objects(runs_tp: dict, bases=(S3, GCS)) -> dict:
    """ECMWF open-data layout: ``runs_tp[run][step]`` = tp (m) on NATIVE; other params pad the files."""
    objs = {}
    for run, steps in runs_tp.items():
        for step in [0, *steps]:
            name = f"{run:%Y%m%d}/{run:%H}z/ifs/0p1/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc-sfc"
            pad = b"x" * 1000  # another parameter before tp
            msg = make_tp(np.asarray(steps.get(step, np.zeros(NATIVE.shape))), run, step) if step else b""
            index = [{"param": "2t", "levtype": "sfc", "step": str(step), "_offset": 0, "_length": len(pad)}]
            if step:
                index.append({"param": "tp", "levtype": "sfc", "step": str(step), "_offset": len(pad),
                              "_length": len(msg)})
            for b in bases:
                objs[b + name + ".grib2"] = pad + msg + b"y" * 500
                objs[b + name + ".index"] = "\n".join(json.dumps(e) for e in index).encode()
    return objs


# --- GRIB ----------------------------------------------------------------------------------------
def test_grib_message_parse_decode_crop():
    vals = np.arange(NATIVE.size, dtype=np.float64).reshape(NATIVE.shape) * 1e-4  # metres
    msg = make_tp(vals, RUN, 3)
    info = parse_grib(msg)
    assert (info.run, info.step0, info.step1, info.stat, info.category, info.number) == (RUN, 0, 3, 1, 1, 193)
    assert info.grid.same_cells(NATIVE) and info.packing == 0 and info.mm_per_unit == 1000.0
    mm, _ = decode_grib_tp(msg, WIN, NATIVE)
    assert mm.shape == WIN.shape
    assert np.allclose(mm, WIN.crop(vals, NATIVE) * 1000.0, atol=1e-3)
    assert mm[0, 0] == pytest.approx(vals[1, 2] * 1000, abs=1e-3)   # window starts one row, two columns in
    with pytest.raises(ValueError, match="not total precipitation"):
        decode_grib_tp(make_tp(vals, RUN, 3, number=52), WIN, NATIVE)
    with pytest.raises(ValueError, match="is not"):
        decode_grib_tp(make_tp(vals, RUN, 3, grid=GridSpec("g", 170.1, -40.0, 0.1, -0.1, 8, 6)), WIN, NATIVE)
    with pytest.raises(ValueError, match="truncated"):
        parse_grib(msg[:-10])


def test_grib_reader_differencing_split_and_byte_ranges():
    a1 = np.full(NATIVE.shape, 0.003)          # 3 mm in 0-3 h
    tp6 = a1 + 0.0045                           # + 4.5 mm in 3-6 h
    tp6[1, 2] = a1[1, 2] - 1e-5                 # packing noise: tp went down by 0.01 mm in one cell
    http = FakeHttp(grib_objects({RUN: {3: a1, 6: tp6}}))
    rd = GribReader(WIN, Bucket([S3, GCS], http), native=NATIVE)
    assert rd.steps(RUN) == (0, 3, 6) and rd.leads(RUN) == (1, 2, 3, 4, 5, 6)
    for lead in (1, 2, 3):
        mm, how = rd.hourly(RUN, lead)
        assert np.allclose(mm, 1.0, atol=1e-4) and how == "tp 0-3 h / 3"
    mm, how = rd.hourly(RUN, 5)
    assert how == "tp 3-6 h / 3" and mm[0, 0] == 0.0               # clipped, not negative
    assert np.allclose(mm.ravel()[1:], 1.5, atol=1e-4)
    # only the tp messages were fetched, by byte range, each once (cached for the 3 hours of a step)
    rng = http.grib_ranges()
    assert len(rng) == 2 and all(r is not None for _, r in rng)
    obj = http.objects[rng[0][0]]
    assert sum(int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for _, r in rng) < 2 * len(obj)
    with pytest.raises(ValueError, match="no step covering"):
        rd.hourly(RUN, 7)
    assert rd.leads(RUN - timedelta(hours=6)) == ()                 # not published


def test_mirrors_retries_and_not_found():
    objs = grib_objects({RUN: {3: np.full(NATIVE.shape, 0.003)}})
    http = FakeHttp(objs, fail=(S3,))                                # S3 answers 503 SlowDown to everything
    rd = GribReader(WIN, Bucket([S3, GCS], http), native=NATIVE)
    mm, _ = rd.hourly(RUN, 2)
    assert np.allclose(mm, 1.0, atol=1e-4) and rd.bucket.base == GCS and http.stats.retries >= 2
    with pytest.raises(NotFound):
        rd.bucket.get("nope.index")                                  # a 404 is an answer: no fallback needed
    with pytest.raises(FetchError):
        Bucket([S3], FakeHttp(objs, fail=(S3,))).get("x")


# --- plans, priorities, windows -----------------------------------------------------------------
class FakeReader(Reader):
    """Hour value = 100 * (run index) + lead, so each slot shows which run and lead filled it."""

    name = "fake"

    def __init__(self, published: dict[datetime, int | set[int]]):
        super().__init__(WIN)
        self.published = published  # run -> last lead published, or the set of leads listed so far
        self.bucket = Bucket(["https://nowhere/"], Http(retries=0))  # run_ecmwf reads its download tally

    def _list_leads(self, run):
        p = self.published.get(run, 0)
        return tuple(sorted(p)) if isinstance(p, set) else tuple(range(1, p + 1))

    def value(self, run, lead):
        return 100.0 * ((hour_index(run) - hour_index(RUN)) // 6 + 10) + lead

    def hourly(self, run, lead):
        assert lead in self.leads(run)
        return np.full(WIN.shape, self.value(run, lead), dtype=np.float32), "fake"


def _weights(grid=WIN, n=2, area=1e6):
    counts = sp.csr_matrix((np.ones(n, np.int32), (np.arange(n), np.arange(n))), shape=(n, grid.size))
    return CatchmentWeights(grid, counts, np.arange(n, dtype=np.int64) + 7, np.ones(n, np.int64), area,
                            np.full(n, -1, np.int64), np.zeros(n, np.int16), ("West Coast",),
                            np.zeros(n, np.float32), np.zeros(n, np.float32))


def test_obs_plan_lead_selection_and_missing_run():
    runs = [RUN - timedelta(hours=6 * k) for k in range(5)]          # RUN, RUN-6 .. RUN-24
    rd = FakeReader({r: 48 for r in runs if r != runs[2]})           # RUN-12 never published
    plan, missing, end = obs_plan(rd, RUN, (1, 6), 5)
    assert end == hour_index(RUN) + 6 and len(plan) + len(missing) == 24
    assert missing == [hour_index(runs[2]) + k for k in range(1, 7)]  # its 6 hours have no run
    assert all(lead in range(1, 7) for _, lead in plan.values())
    assert plan[end] == (RUN, 6) and plan[end - 6] == (runs[1], 6)
    plan12, missing12, _ = obs_plan(rd, RUN, (1, 12), 5)             # longer leads fill the gap from RUN-18
    assert not missing12
    assert plan12[hour_index(runs[2]) + 1] == (runs[3], 7)
    assert plan12[end] == (RUN, 6)                                    # the newest run (shortest lead) wins


def test_obs_ingest_priority_and_no_double_count():
    runs = [RUN - timedelta(hours=6 * k) for k in range(5)]
    rd, w = FakeReader({r: 48 for r in runs}), _weights()
    buf = HourlyBuffer.for_weights(w, "observed")
    plan, missing, end = obs_plan(rd, RUN, (1, 6), 5)
    first = ingest(EcmwfSource(rd, "ECMWF_OBS", plan), buf, w, hour_time(end - 24), hour_time(end))
    assert {r.status for r in first} == {"new"} and not missing
    total = buf.window(24).mm.copy()
    expected = sum(rd.value(r, lead) for r, lead in plan.values())
    assert np.allclose(total, expected)
    again = ingest(EcmwfSource(rd, "ECMWF_OBS", plan), buf, w, hour_time(end - 24), hour_time(end))
    assert {r.status for r in again} == {"updated"} and np.allclose(buf.window(24).mm, total)  # same, not doubled
    row = buf.slot_table()[-1]
    assert (row["product"], row["source"], row["lead"]) == ("ECMWF_OBS", "fake", 6)
    assert row["run"] == RUN.strftime("%Y-%m-%d %H:00Z")
    # an older run with a longer lead never replaces a newer run's short lead for the same hour
    old = {end: (runs[1], 12)}
    assert [r.status for r in ingest(EcmwfSource(rd, "ECMWF_OBS", old), buf, w, hour_time(end - 1),
                                     hour_time(end))] == ["skipped"]
    # a better (higher-priority) product of another family is not overwritten by ECMWF_OBS
    sat = HourlyBuffer.for_weights(w, "observed")
    sat.put(end, np.full(2, 9.0), "GSMAP_NRT")
    assert ingest(EcmwfSource(rd, "ECMWF_OBS", {end: (RUN, 6)}), sat, w, hour_time(end - 1),
                  hour_time(end))[0].status == "skipped"
    with pytest.raises(ValueError, match="forecast product"):
        ingest(EcmwfSource(rd, "ECMWF_FC", {end: (RUN, 6)}), buf, w, hour_time(end - 1), hour_time(end))


def test_forecast_plan_find_run_and_windows():
    newer = RUN + timedelta(hours=6)
    rd, w = FakeReader({RUN: 144, newer: 10}), _weights()             # the newer run is still being published
    now = hour_index(RUN) + 8
    need = lambda r: range(max(1, now + 1 - hour_index(r)), now + 25 - hour_index(r))  # noqa: E731
    assert find_run(rd, hour_time(now), need) == RUN                  # newer one lacks leads 3..26
    assert find_run(rd, hour_time(now), lambda r: range(1, 7)) == newer
    plan, missing = fc_plan(rd, RUN, now)
    assert not missing and [plan[h][1] for h in sorted(plan)] == list(range(9, 33))
    fc = HourlyBuffer.for_weights(w, "forecast")
    fc.advance(now)
    res = ingest(EcmwfSource(rd, "ECMWF_FC", plan), fc, w, hour_time(now), hour_time(now + 24))
    assert {r.status for r in res} == {"new"}
    for x in range(1, 25):
        t = fc.ahead(x)
        assert t.all_complete and np.allclose(t.mm, sum(rd.value(RUN, 8 + k) for k in range(1, x + 1)))
    assert np.allclose(fc.ahead(6).m3(w.area_m2), fc.ahead(6).mm / 1000 * 1e6)
    assert set(fc.run_hour[fc.hour >= 0]) == {hour_index(RUN)}
    short, miss = fc_plan(rd, newer, now)                             # a run that does not reach far enough
    assert len(short) == 8 and len(miss) == 16                       # leads 3..10 only


# --- the observed window ends at now ------------------------------------------------------------------
def _cfg(root, **ecmwf):
    """Config on ``root`` (defaults of [ecmwf] unless given) with the WIN weights in place."""
    (root / "rain").mkdir(parents=True, exist_ok=True)
    _weights().save(root / "rain" / "weights_ecmwf.npz")
    return parse_config({"observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
                         "grids": {"ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133,
                                             "ny": 141}},
                         "paths": {"nz_data": str(root)}, "ecmwf": ecmwf})


def _h(h: int) -> str:
    return hour_time(h).strftime("%Y-%m-%d %H:00Z")


def _run_of(label: str) -> datetime:
    return datetime.strptime(label, "%Y-%m-%d %HZ").replace(tzinfo=UTC)


def test_observed_window_ends_now_next_to_the_forecast(tmp_path):
    """Through a whole run cycle ("now" 5-11 h after RUN, each run fully out 5 h after its base time) the
    window ends at now: hours after the latest run come from its own leads, the rest from leads 1-6."""
    cfg = _cfg(tmp_path)                                              # obs_leads 1-18, 6 runs
    runs = [RUN + timedelta(hours=6 * k) for k in range(-6, 2)]       # RUN-36 .. RUN+6
    for k in range(5, 12):
        now_h = hour_index(RUN) + k
        rd = FakeReader({r: 90 for r in runs if hour_index(r) + 5 <= now_h})
        rep = run_ecmwf(cfg, reader=rd, now=hour_time(now_h), log=lambda m: None)
        o, f = rep["observed"], rep["forecast"]
        latest = RUN if k < 11 else RUN + timedelta(hours=6)          # RUN+6 is out at RUN+11
        assert o["latest_run"] == f["run"] == f"{latest:%Y-%m-%d %H}Z"
        assert o["window_end"] == o["now"] == _h(now_h) and o["hours_behind_now"] == 0 and not o["not_published"]
        assert len(o["hours"]) == 24 and not o["missing"] and o["hours"][0]["hour"] == _h(now_h - 23)
        assert o["hours"][-1]["hour"] == _h(now_h) and f["hours"][0]["hour"] == _h(now_h + 1)   # no hole
        own = [h["lead"] for h in o["hours"] if _run_of(h["run"]) == latest]
        assert own == list(range(1, now_h - hour_index(latest) + 1)) and o["latest_run_leads"] == [1, own[-1]]
        assert all(1 <= h["lead"] <= 6 for h in o["hours"] if _run_of(h["run"]) != latest)
        assert o["windows"]["1"]["last"] == _h(now_h) and not o["windows"]["24"]["missing_hours"]
    # the saved buffer holds exactly the last plan: newer runs replaced the long leads of older ones
    buf = HourlyBuffer.load(cfg.buffer_path("observed", "ecmwf"))
    want = sum(rd.value(_run_of(h["run"]), h["lead"]) for h in o["hours"])
    assert buf.anchor == now_h and np.allclose(buf.window(24).mm, want)


def test_observed_window_with_a_half_converted_newest_run(tmp_path):
    new = RUN + timedelta(hours=6)
    now_h = hour_index(new) + 5                                       # its first leads are out, the rest not yet
    older = [RUN - timedelta(hours=6 * k) for k in range(6)]          # RUN .. RUN-30
    logs = []
    rd, cfg = FakeReader({new: 3, **{r: 90 for r in older}}), _cfg(tmp_path / "a")
    rep = run_ecmwf(cfg, reader=rd, now=hour_time(now_h), log=logs.append)
    o, f = rep["observed"], rep["forecast"]
    assert o["latest_run"] == f"{new:%Y-%m-%d %H}Z" and o["latest_run_leads"] == [1, 3]
    assert o["window_end"] == _h(now_h) and o["hours_behind_now"] == 0 and not o["missing"]
    assert [(_run_of(h["run"]), h["lead"]) for h in o["hours"][-5:]] == [(new, 1), (new, 2), (new, 3), (RUN, 10),
                                                                          (RUN, 11)]
    assert f["run"] == f"{RUN:%Y-%m-%d %H}Z" and f["hours"][0]["hour"] == _h(now_h + 1)   # new lacks its leads
    assert json.loads(cfg.buffer_path("observed", "ecmwf").with_suffix(".json").read_text())["window_end"] == _h(now_h)
    # an outage: RUN, RUN-6 and RUN-12 never came out. Hours no run publishes are reported: inside the window as
    # missing, after its end (no run reaches now within leads 1-18) as not published, and the window lags now
    rd = FakeReader({new: {1, 2, 3}, **{r: 90 for r in older[3:]}})
    o = run_ecmwf(_cfg(tmp_path / "b"), reader=rd, now=hour_time(now_h), fc=False, log=logs.append)["observed"]
    run_h = hour_index(RUN)
    assert o["window_end"] == _h(run_h + 9) and o["hours_behind_now"] == 2
    assert o["not_published"] == [_h(run_h + 10), _h(run_h + 11)]
    assert o["missing"] == [_h(run_h + k) for k in range(1, 7)]       # RUN-18 reaches lead 18 = RUN, no further
    assert o["windows"]["24"]["missing_hours"] == o["missing"] and len(o["hours"]) == 18
    assert {(_run_of(h["run"]), h["lead"]) for h in o["hours"][-3:]} == {(new, 1), (new, 2), (new, 3)}
    assert any("no run publishes these hours" in m for m in logs)


def test_grib_observed_window_with_a_half_published_newest_run(tmp_path):
    """ecmwf-s3: 3-hourly steps split evenly, with the wider leads: the newest run has steps 3 and 6 only, so
    the two hours after them come from RUN's 12-15 h step, and the window still ends at now."""
    new = RUN + timedelta(hours=6)
    rate = {RUN - timedelta(hours=6 * k): 0.001 * (k + 1) for k in range(5)} | {new: 0.01}   # m per hour
    tp = {r: {s: np.full(NATIVE.shape, rate[r] * s) for s in range(3, 25, 3)} for r in rate if r != new}
    tp[new] = {s: np.full(NATIVE.shape, rate[new] * s) for s in (3, 6)}
    cfg = _cfg(tmp_path, reader="ecmwf-s3")
    now_h = hour_index(new) + 8
    rd = GribReader(WIN, Bucket([S3, GCS], FakeHttp(grib_objects(tp))), native=NATIVE)
    o = run_ecmwf(cfg, reader=rd, now=hour_time(now_h), fc=False, log=lambda m: None)["observed"]
    assert o["latest_run"] == f"{new:%Y-%m-%d %H}Z" and o["latest_run_leads"] == [1, 6]
    assert o["window_end"] == _h(now_h) and o["hours_behind_now"] == 0 and not o["missing"] and len(o["hours"]) == 24
    assert [(_run_of(h["run"]), h["lead"], h["method"]) for h in o["hours"][-2:]] == [
        (RUN, 13, "tp 12-15 h / 3"), (RUN, 14, "tp 12-15 h / 3")]
    assert [h["lead"] for h in o["hours"][-8:-2]] == list(range(1, 7))
    assert o["windows"]["1"]["mean_mm"] == pytest.approx(rate[RUN] * 1000, rel=1e-4)
    assert o["windows"]["3"]["mean_mm"] == pytest.approx((2 * rate[RUN] + rate[new]) * 1000, rel=1e-4)
    assert o["windows"]["24"]["mean_mm"] == pytest.approx(
        (6 * rate[new] + 8 * rate[RUN] + 6 * rate[RUN - timedelta(hours=6)] + 4 * rate[RUN - timedelta(hours=12)])
        * 1000, rel=1e-4)


# --- Open-Meteo (.om on an octahedral grid) ------------------------------------------------------
def _om_file(tmp_path, values, run, lead, n=8, crs_n=None):
    import omfiles

    p = tmp_path / f"om_{lead}_{crs_n or n}.om"
    wr = omfiles.OmFileWriter(str(p))
    a = wr.write_array(np.asarray(values, np.float32).reshape(1, -1), chunks=[1, 64], scale_factor=1000.0,
                       add_offset=0.0, compression="pfor_delta_2d", name="precipitation")
    c = wr.write_scalar(f"GEOGCRS[\"Reduced Gaussian Grid\", REMARK[\"Reduced Gaussian Grid O{crs_n or n}\"]]",
                        name="crs_wkt")
    t = wr.write_scalar(np.int64(run.timestamp()), name="forecast_reference_time")
    v = wr.write_scalar(np.int64((run + timedelta(hours=lead)).timestamp()), name="valid_time")
    wr.close(wr.write_group("", children=[a, c, t, v]))
    return p.read_bytes()


def test_octahedral_grid_and_openmeteo_reader(tmp_path):
    og = OctahedralGrid(8)
    assert list(og.nlon[:3]) == [20, 24, 28] and og.nlon[7] == og.nlon[8] == 48 and og.size == 2 * sum(
        4 * i + 16 for i in range(1, 9))
    assert np.all(np.diff(og.lat) < 0) and abs(og.lat[0] + og.lat[-1]) < 1e-9
    win = GridSpec("om_win", lon0=170.0, lat0=-30.0, dlon=1.0, dlat=-1.0, nx=5, ny=6)
    lon = np.concatenate([np.arange(k) * 360.0 / k for k in og.nlon])
    lat = np.repeat(og.lat, og.nlon)
    field = lambda lead: 0.5 * lead + 0.01 * lon - 0.02 * lat  # noqa: E731  linear: bilinear is exact away from 0°
    objs = {}
    for lead in range(0, 4):
        key = f"data_spatial/ecmwf_ifs/{RUN:%Y/%m/%d/%H}00Z/{RUN + timedelta(hours=lead):%Y-%m-%dT%H%M}.om"
        objs[OM + key] = _om_file(tmp_path, field(lead), RUN, lead)
    http = FakeHttp(objs)
    rd = OpenMeteoReader(win, Bucket([OM], http), ogrid=og)
    assert rd.leads(RUN) == (1, 2, 3)
    LON, LAT = np.meshgrid(win.lons, win.lats)
    mm, how = rd.hourly(RUN, 2)
    assert how.startswith("hourly") and np.allclose(mm, 1.0 + 0.01 * LON - 0.02 * LAT, atol=2e-3)
    assert all(r is not None for u, r in http.calls if u.endswith(".om"))  # range reads only
    assert sum(u.endswith(".om") for u, _ in http.calls) <= 3
    with pytest.raises(ValueError, match="no hourly"):
        rd.hourly(RUN, 4)
    bad = dict(objs)
    k = next(k for k in objs if k.endswith("T0800.om"))
    bad[k] = _om_file(tmp_path, field(2), RUN, 2, crs_n=9)
    with pytest.raises(ValueError, match="not O8"):
        OpenMeteoReader(win, Bucket([OM], FakeHttp(bad)), ogrid=og).hourly(RUN, 2)
    bad[k] = _om_file(tmp_path, field(2), RUN, 3)                       # valid_time says lead 3
    with pytest.raises(ValueError, match="valid_time"):
        OpenMeteoReader(win, Bucket([OM], FakeHttp(bad)), ogrid=og).hourly(RUN, 2)


# --- CLI step end to end -------------------------------------------------------------------------
def test_run_ecmwf_twice_does_not_double_count(tmp_path):
    runs = [RUN - timedelta(hours=6 * k) for k in range(4)]
    rate = {r: 0.001 * (k + 1) for k, r in enumerate(runs)}          # m per hour, a different rate per run
    tp = {r: {s: np.full(NATIVE.shape, rate[r] * s) for s in range(3, 37, 3)} for r in runs}
    http = FakeHttp(grib_objects(tp))
    w = _weights()
    (tmp_path / "rain").mkdir()
    w.save(tmp_path / "rain" / "weights_ecmwf.npz")
    cfg = parse_config({"observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
                        "grids": {"ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133,
                                            "ny": 141}},
                        "paths": {"nz_data": str(tmp_path)}, "ecmwf": {"obs_runs": 4, "reader": "ecmwf-s3"}})
    now = RUN + timedelta(hours=9)
    logs = []

    def go():
        # keep_days: the cache is pruned against the real clock, and these runs (2026-11-18/19) age past 3 days
        rd = GribReader(WIN, Bucket([S3, GCS], http), native=NATIVE, cache_dir=tmp_path / "rain" / "cache",
                        keep_days=36500)
        return run_ecmwf(cfg, reader=rd, now=now, log=logs.append)

    rep = go()
    o, f = rep["observed"], rep["forecast"]
    assert o["latest_run"] == "2026-11-19 06Z" and len(o["hours"]) == 24 and not o["missing"]
    # the window ends at now (RUN + 9): leads 1-9 of the latest run (7-9 split from its 6-9 h step), 1-6 before
    assert o["window_end"] == o["now"] == "2026-11-19 15:00Z" and o["hours_behind_now"] == 0
    assert not o["not_published"] and o["latest_run_leads"] == [1, 9]
    assert [h["lead"] for h in o["hours"] if h["run"] == "2026-11-19 06Z"] == list(range(1, 10))
    assert {h["lead"] for h in o["hours"] if h["run"] != "2026-11-19 06Z"} == set(range(1, 7))
    assert o["hours"][-1]["method"] == "tp 6-9 h / 3" and o["hours"][-1]["hour"] == "2026-11-19 15:00Z"
    assert {h["status"] for h in o["hours"]} == {"new"}
    assert f["run"] == "2026-11-19 06Z" and f["run_age_hours"] == 9 and len(f["hours"]) == 24
    assert [h["lead"] for h in f["hours"]] == list(range(10, 34)) and f["hours"][0]["method"] == "tp 9-12 h / 3"
    assert f["hours"][0]["hour"] == "2026-11-19 16:00Z"                # no hour between observed and forecast
    assert o["scaled"] is f["scaled"] is False and o["factor_versions"] == f["factor_versions"] == [0]
    # 24 h observed = 9 h of the latest run, 6 h of each of the next two and 3 h of the oldest, each at its own
    # rate; 24 h forecast = 24 h at the latest rate
    obs_h = {runs[0]: 9, runs[1]: 6, runs[2]: 6, runs[3]: 3}
    assert o["windows"]["24"]["mean_mm"] == pytest.approx(sum(obs_h[r] * rate[r] for r in runs) * 1000, rel=1e-4)
    assert f["windows"]["24"]["mean_mm"] == pytest.approx(24 * rate[RUN] * 1000, rel=1e-4)
    obs1 = HourlyBuffer.load(cfg.buffer_path("observed", "ecmwf")).window(24).mm
    fc1 = HourlyBuffer.load(cfg.buffer_path("forecast", "ecmwf")).ahead(24).mm
    assert json.loads(cfg.buffer_path("forecast", "ecmwf").with_suffix(".json").read_text())["run"] == f["run"]
    n_grib = len(http.grib_ranges())
    rep2 = go()                                                       # same run, same hour: nothing added
    assert {h["status"] for h in rep2["observed"]["hours"] + rep2["forecast"]["hours"]} == {"updated"}
    assert np.allclose(HourlyBuffer.load(cfg.buffer_path("observed", "ecmwf")).window(24).mm, obs1)
    assert np.allclose(HourlyBuffer.load(cfg.buffer_path("forecast", "ecmwf")).ahead(24).mm, fc1)
    assert len(http.grib_ranges()) == n_grib                          # decoded fields came from the cache
    lines = []
    status(cfg, log=lines.append)
    assert any("forecast ecmwf" in s and "2026-11-19 06:00Z" in s for s in lines)
    assert parse_run("2026111912") == RUN + timedelta(hours=6)
    with pytest.raises(ValueError, match="00, 06, 12 and 18"):
        parse_run("2026111907")


def test_ecmwf_config_validation():
    base = {"grids": {"ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133, "ny": 141}},
            "observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"}}
    c = parse_config(base)
    assert c.ecmwf.reader == "openmeteo-s3" and c.ecmwf.obs_leads == (1, 18) and c.ecmwf.obs_runs == 6
    assert c.cache_dir.name == "cache"
    shipped = load_config()                                            # config/rainfall.toml says the same
    assert (shipped.ecmwf.obs_leads, shipped.ecmwf.obs_runs) == (c.ecmwf.obs_leads, c.ecmwf.obs_runs)
    assert parse_config({**base, "ecmwf": {"reader": "ecmwf-s3", "obs_leads": [1, 12], "cache": ""}}).cache_dir is None
    for bad, msg in [({"reader": "ftp"}, "reader"), ({"obs_leads": [1, 3]}, "at least 6"),
                     ({"obs_leads": [0, 6]}, "obs_leads"), ({"obs_runs": 0}, "obs_runs"), ({"bogus": 1}, "unknown"),
                     ({"ecmwf_urls": []}, "ecmwf_urls")]:
        with pytest.raises(ValueError, match=msg):
            parse_config({**base, "ecmwf": bad})

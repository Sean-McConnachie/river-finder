"""Live rain (rf-t26.9) without network: the region file encoding, the inputs manifest, the publish step
and the exit code the hourly job keys on."""

import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from river_finder import cli
from river_finder.rainfall import (
    CatchmentWeights,
    GridSpec,
    HourlyBuffer,
    Network,
    UpstreamTotals,
    compute_totals,
    hour_index,
)
from river_finder.rainfall import pipeline as pl
from river_finder.rainfall.config import parse_config
from river_finder.rainfall.ecmwf import Reader, obs_plan
from river_finder.rainfall.fetch import Bucket, FetchError, Http
from river_finder.rainfall.live import (
    MAGIC,
    decode_region,
    encode_region,
    quantize_area,
    quantize_mm,
    region_columns,
)

T0 = datetime(2026, 11, 19, 0, tzinfo=UTC)
H0 = hour_index(T0)
#   A   B
#    \ /
#     C -> D -> E (outlet)    G (outlet)    H -> I (outlet at a tile edge)
#               ^
#           F --'
ROW = {"E": 0, "A": 1, "D": 2, "G": 3, "C": 4, "F": 5, "B": 6, "I": 7, "H": 8}
DOWN = {"E": None, "A": "C", "D": "E", "G": None, "C": "D", "F": "E", "B": "C", "I": None, "H": "I"}
AREA_KM2 = {"E": 5.0, "A": 1.0, "D": 2.0, "G": 7.0, "C": 3.0, "F": 4.0, "B": 1.5, "I": 2.0, "H": 6.0}
REGIONS = ("Bay of Plenty", "Hawke's Bay")
REGION = {"E": 0, "A": 0, "D": 0, "G": 1, "C": 0, "F": 1, "B": 0, "I": 1, "H": 1}
VI = {"E": 1, "A": -1, "D": 0, "G": 0, "C": 2, "F": -1, "B": -1, "I": 1, "H": -1}
N = len(ROW)
NAMES = sorted(ROW, key=ROW.get)
DOWN_ROWS = np.array([-1 if DOWN[k] is None else ROW[DOWN[k]] for k in NAMES], dtype=np.int64)
AREA_M2 = np.array([AREA_KM2[k] for k in NAMES]) * 1e6
REGION_OF = np.array([REGION[k] for k in NAMES], dtype=np.int16)
VI_COL = np.array([VI[k] for k in NAMES], dtype=np.int32)
GID = np.arange(N, dtype=np.int64) * 7 + 1000
TOY = GridSpec("toy", lon0=0.5, lat0=0.5, dlon=1.0, dlat=1.0, nx=2, ny=1)
ECMWF = GridSpec("ecmwf", lon0=165.8, lat0=-33.8, dlon=0.1, dlat=-0.1, nx=133, ny=141)    # config/rainfall.toml
CFG = {"observed": {"source": "ecmwf"}, "forecast": {"source": "ecmwf"},
       "grids": {"ecmwf": {"lon0": 165.8, "lat0": -33.8, "dlon": 0.1, "dlat": -0.1, "nx": 133, "ny": 141}}}


def _upstream(local):
    out = np.zeros(N)
    for c in range(N):
        r = c
        while r >= 0:
            out[r] += local[c]
            r = DOWN_ROWS[r]
    return out


def _totals(forecast=True):
    net = Network.build(GID, DOWN_ROWS, AREA_M2, REGION_OF, REGIONS, _upstream(AREA_M2) / 1e6, VI_COL,
                        edge_gids=[GID[ROW["I"]]])
    rng = np.random.default_rng(3)
    obs = HourlyBuffer(N, "observed", REGIONS, REGION_OF)
    for h in range(H0, H0 + 24):
        v = rng.gamma(0.8, 4.0, N).astype(np.float32)
        if h == H0 + 22:
            v[ROW["A"]] = np.nan                       # one catchment-hour missing: miss varies per reach
        obs.put(h, v, "ECMWF_OBS", run_hour=h - 3, lead=3)
    bufs = {"observed": obs}
    if forecast:
        fc = HourlyBuffer(N, "forecast", REGIONS, REGION_OF)
        fc.advance(H0 + 23)
        for h in range(H0 + 24, H0 + 48):
            fc.put(h, np.full(N, 0.25, np.float32), "ECMWF_FC", run_hour=H0 + 18, lead=h - H0 - 18)
        bufs["forecast"] = fc
    return compute_totals(net, bufs, (1, 3, 6, 12, 24))


# --- the region file ------------------------------------------------------------------------------
def test_quantizers_cover_the_edges():
    q = quantize_mm(np.array([np.nan, 0.0, 0.049, 0.051, 12.34, -1e-6, 7000.0]))
    assert q.dtype == np.dtype("<u2") and q.tolist() == [0, 1, 1, 2, 124, 1, 65535]   # 0 = null; capped
    a = np.array([1e-5, 0.5, 2.0, 18923.5, 2.8e5])
    back = 10 ** (quantize_area(a) * 2e-4 - 5)
    assert np.all(np.abs(back / a - 1) < 2.4e-4)                                      # 0.05 % steps
    assert quantize_area(np.array([0.0]))[0] == 0                                     # clipped at 1e-5 km2


def test_region_file_round_trip_and_layout():
    t = _totals()
    rows = t.viewer_rows()["Bay of Plenty"]
    assert [NAMES[r] for r in rows] == ["D", "E", "C"]                                # vi order
    header = {"schema": 1, "region": "bay-of-plenty", "n": int(rows.size), "generated": "x"}
    raw = encode_region(header, region_columns(t, rows))
    blob = gzip.compress(raw, mtime=0)
    assert raw[:4] == MAGIC
    h, d = np.frombuffer(raw, "<u4", 2, 4)
    assert d % 8 == 0 and d >= 12 + h
    head, f = decode_region(blob)                                                     # gzip or plain
    assert decode_region(raw)[0] == head and head["region"] == "bay-of-plenty" and head["n"] == 3
    fields = {x["k"]: x for x in head["fields"]}
    for x in fields.values():
        assert ("const" in x) != ("off" in x)
        if "off" in x and x["kind"] != "u8":
            assert (d + x["off"]) % 2 == 0                                            # Uint16Array views
    for kind, p in (("observed", "obs"), ("forecast", "fc")):
        for x in t.windows:
            assert np.abs(f[f"{p}_mm_{x}h"] - t.mm(kind, x)[rows]).max() <= 0.05 + 1e-9
            assert (f[f"{p}_miss_{x}h"] == t.miss_hours(kind, x)[rows]).all()
            k = t.kind(kind)
            assert (f[f"{p}_prod_{x}h"] == k.products[k.index(x), rows]).all()
            assert (f[f"{p}_scale_{x}h"] == k.scale[k.index(x), rows]).all()
    assert np.allclose(f["area_km2"], t.up_area_m2[rows] / 1e6, rtol=2.4e-4)
    assert (f["flags"] == t.flags[rows]).all()
    # the missing catchment-hour (A, 2 h before the end) counts in every reach below A: C, D and E
    assert f["obs_miss_1h"].tolist() == [0, 0, 0] and f["obs_miss_3h"].tolist() == [1, 1, 1]
    assert "off" in fields["obs_mm_24h"] and fields["fc_mm_6h"]["const"] == pytest.approx(1.5)   # 6 h x 0.25 mm
    assert fields["fc_miss_24h"]["const"] == 0 and fields["obs_prod_24h"]["const"] == 1
    m3 = f["obs_mm_24h"] * f["area_km2"] * 1000
    assert np.allclose(m3, t.m3("observed", 24)[rows], rtol=0.01, atol=0.05 * f["area_km2"] * 1000)


def test_region_file_without_forecast_and_bad_input():
    t = _totals(forecast=False)
    rows = t.viewer_rows()["Hawke's Bay"]
    head, f = decode_region(encode_region({"n": int(rows.size)}, region_columns(t, rows)))
    fields = {x["k"]: x for x in head["fields"]}
    assert fields["fc_mm_24h"]["const"] is None and np.isnan(f["fc_mm_24h"]).all()   # null: no forecast
    assert not np.isnan(f["obs_mm_24h"]).any()
    with pytest.raises(ValueError, match="values for"):
        encode_region({"n": 5}, region_columns(t, rows))
    with pytest.raises(ValueError, match="not a river-finder"):
        decode_region(b"PK\x03\x04" + b"\0" * 20)


# --- inputs, totals and publish through the CLI ------------------------------------------------------
def _national(root, grid=ECMWF):
    """A national data directory: weights, segment table, relink log, viewer index; no network.npz."""
    px = 1e4
    npix = (AREA_M2 / px).astype(np.int64)
    counts = sp.csr_matrix((npix.astype(np.int32), (np.arange(N), np.arange(N) % 2)), shape=(N, grid.size))
    tab = _upstream(AREA_M2) / 1e6
    w = CatchmentWeights(grid, counts, GID, npix, px, DOWN_ROWS, REGION_OF, REGIONS, tab.astype(np.float32),
                         (AREA_M2 / 1e6).astype(np.float32), {"built": "2026-11-20T01:17:48Z"})
    data = root / "data" / "nz"
    (data / "rain").mkdir(parents=True)
    w.save(data / "rain" / "weights_ecmwf.npz")
    pd.DataFrame({"gid": GID, "down": DOWN_ROWS, "vi": VI_COL.astype(np.int64), "area_km2": tab,
                  "region": [REGIONS[r] for r in REGION_OF]}).to_pickle(data / "segments_table.pkl")
    pd.DataFrame({"gid": [GID[ROW["I"]]], "status": ["no reach on the REC2 path"]}).to_csv(
        data / "relink_log.csv", index=False)
    viewer = root / "data" / "viewer_nz"
    viewer.mkdir()
    index = [{"id": "bay-of-plenty", "title": "Bay of Plenty", "group": "River-guide regions", "segments": 3},
             {"id": "hawkes-bay", "title": "Hawke's Bay", "group": "River-guide regions", "segments": 2},
             {"id": "kaimai", "title": "Kaimai", "group": "Reference areas (5 m LiDAR)", "segments": 6730}]
    (viewer / "regions_index.json").write_text(json.dumps(index))
    return data, viewer, w


def _field(a, b):
    v = np.zeros(ECMWF.size)
    v[:2] = a, b
    return v


def _buffers(w, rain_dir, now_h):
    obs = HourlyBuffer.for_weights(w, "observed")
    for h in range(now_h - 23, now_h + 1):
        obs.put(h, w.mm(_field(1.0, 2.0)), "ECMWF_OBS", run_hour=h - 3, lead=3)
    obs.save(rain_dir / "obs_ecmwf.npz")
    fc = HourlyBuffer.for_weights(w, "forecast")
    fc.advance(now_h)
    for h in range(now_h + 1, now_h + 25):
        fc.put(h, w.mm(_field(0.5, 0.5)), "ECMWF_FC", run_hour=now_h - 5, lead=h - now_h + 5)
    fc.save(rain_dir / "fc_ecmwf.npz")


def test_inputs_manifest_then_job_publishes(tmp_path, monkeypatch):
    data, viewer, w = _national(tmp_path)
    before = sorted(p.name for p in (data / "rain").iterdir())
    out = tmp_path / "rain-inputs"
    cli.main(["rain", "inputs", "--data", str(data), "--out", str(out), "--viewer", str(viewer)])
    assert sorted(p.name for p in (data / "rain").iterdir()) == before              # the data dir is only read
    m = json.loads((out / "manifest.json").read_text())
    assert m["format"] == "river-finder rain inputs v1" and m["rows"] == N
    paths = {f["path"] for f in m["files"]}
    assert paths == {"rain/weights_ecmwf.npz", "rain/network.npz", "rain/viewer.json"}
    for f in m["files"]:
        p = out / f["path"]
        assert p.stat().st_size == f["bytes"] and hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"]
    assert m["total_bytes"] == sum(f["bytes"] for f in m["files"])
    nb = m["national_build"]
    assert nb["segments_table"]["file"] == "segments_table.pkl" and nb["weights"]["ecmwf"]["built"]
    assert nb["network_built"] and nb["viewer_index"]["file"] == "regions_index.json"
    assert m["regions"] == ["bay-of-plenty", "hawkes-bay"] and list(m["omitted"]) == ["kaimai"]
    assert m["config"]["grids"]["ecmwf"]["nx"] == nb["weights"]["ecmwf"]["grid"]["nx"] == 133
    assert m["config"]["windows"] == [1, 3, 6, 12, 24]
    assert str(tmp_path) not in (out / "manifest.json").read_text()                 # no local paths
    net = Network.load(out / "rain" / "network.npz")                                # built from the table
    assert net.n == N and net.flag_counts()["tile_edge_outlet"] == 1
    vm = json.loads((out / "rain" / "viewer.json").read_text())
    assert [(r["id"], r["name"], r["n"]) for r in vm["regions"]] == [("bay-of-plenty", "Bay of Plenty", 3),
                                                                      ("hawkes-bay", "Hawke's Bay", 2)]
    # the job: a fresh copy of the inputs (no segment table), buffers from "rain ecmwf", then totals + publish
    job = tmp_path / "job"
    shutil.copytree(out, job)
    now_h = hour_index(datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
    _buffers(w, job / "rain", now_h)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    cli.main(["rain", "totals", "--data", str(job)])
    pub = tmp_path / "pub"
    cli.main(["rain", "publish", "--data", str(job), "--out", str(pub)])
    idx = json.loads((pub / "index.json").read_text())
    assert idx["schema"] == 1 and idx["windows"] == [1, 3, 6, 12, 24]
    assert [r["id"] for r in idx["regions"]] == ["bay-of-plenty", "hawkes-bay"] and "kaimai" in idx["omitted"]
    assert idx["observed"]["hours_behind_now"] == 0 and idx["forecast"]["run_age_hours"] >= 5
    assert idx["observed"]["window_end"] == idx["now"] == idx["forecast"]["start"]   # past and next meet at now
    # no gauge factors yet (all version 0): nothing reads "scaled", the per-reach bits say raw
    assert idx["observed"]["scaled"] is idx["forecast"]["scaled"] is idx["totals"]["scaled"] is False
    assert idx["observed"]["scale"] == idx["forecast"]["scale"] == "raw" and idx["totals"]["values"] == "scaled"
    runs = {r["run"]: r for r in idx["ecmwf"]["runs"]}
    assert idx["ecmwf"]["newest_run"] == max(runs) and "forecast" in runs[idx["forecast"]["run"]]["used_for"]
    assert "ECMWF" in idx["attribution"] and "Open-Meteo" not in idx["attribution"]   # no openmeteo-s3 source
    assert idx["inputs"]["rows_digest"] == m["rows_digest"] and idx["job"]["run"].endswith("/o/r/actions/runs/42")
    assert idx["summary"].startswith("Rain ") and idx["forecast"]["run"] in idx["summary"]
    t = UpstreamTotals.load(job / "rain" / "totals.npz")
    for r in idx["regions"]:
        blob = (pub / r["file"]).read_bytes()
        assert len(blob) == r["bytes"] and hashlib.sha256(blob).hexdigest() == r["sha256"]
        head, f = decode_region(blob)
        rows = t.viewer_rows()[head["title"]]
        assert head["n"] == r["n"] == rows.size and head["generated"] == idx["generated"]
        assert np.abs(f["obs_mm_24h"] - t.mm("observed", 24)[rows]).max() <= 0.05 + 1e-9
        assert np.abs(f["fc_mm_6h"] - t.mm("forecast", 6)[rows]).max() <= 0.05 + 1e-9
        assert all((f[f"{p}_scale_{x}h"] == 1).all() for p in ("obs", "fc") for x in idx["windows"])   # 1 = raw
    # inputs that no longer match the site's data are refused
    index = json.loads((viewer / "regions_index.json").read_text())
    index[0]["segments"] = 4
    (viewer / "regions_index.json").write_text(json.dumps(index))
    with pytest.raises(SystemExit, match="different national builds"):
        cli.main(["rain", "inputs", "--data", str(data), "--out", str(tmp_path / "again"), "--viewer", str(viewer)])
    with pytest.raises(SystemExit, match="data directory itself"):
        cli.main(["rain", "inputs", "--data", str(data), "--out", str(data)])
    toy, _, _ = _national(tmp_path / "toy", grid=TOY)                                # weights on another grid
    with pytest.raises(SystemExit, match="another grid"):
        cli.main(["rain", "inputs", "--data", str(toy), "--out", str(tmp_path / "toy-out")])


# --- the exit code of rain ecmwf ---------------------------------------------------------------------
class _Nothing(Reader):
    name = "nothing"

    def __init__(self, grid):
        super().__init__(grid)
        self.bucket = Bucket(["https://nowhere/"], Http(retries=0))

    def _list_leads(self, run):
        return ()

    def hourly(self, run, lead):
        raise AssertionError("nothing is published")


def test_inputs_carry_the_scaling_zones(tmp_path):
    import shapely

    from river_finder.rainfall import live
    from river_finder.rainfall import zones as Z

    data, viewer, w = _national(tmp_path)
    split = {"region": "Hawke's Bay", "divide_km": 15.0, "near": "Hawke's Bay near", "far": "Hawke's Bay far"}
    cfg = parse_config(dict(CFG, paths={"nz_data": str(data)},
                            scaling={"zones": [split], "divide": {"west": ["Bay of Plenty"], "east": ["Hawke's Bay"]}}))
    with pytest.raises(SystemExit, match="run river-finder rain zones"):    # no zone file yet
        live.build_inputs(cfg, tmp_path / "in0", viewer_dir=viewer, log=lambda m: None)
    xy = np.column_stack([np.where(np.isin(np.arange(N), [ROW["G"], ROW["F"]]), 5e3, 40e3), np.zeros(N)])
    zm = Z.build_zones(cfg.scaling.zones, cfg.scaling.divide_west, cfg.scaling.divide_east, xy, w.regions,
                       w.region_of, w.rows_digest, shapely.LineString([(0, -1e5), (0, 1e5)]))
    zm.save(cfg.zones_path)
    m = live.build_inputs(cfg, tmp_path / "in", viewer_dir=viewer, log=lambda m: None)
    paths = {f["path"] for f in m["files"]}
    assert {"rain/scaling_zones.npz", "rain/weights_ecmwf.npz", "rain/network.npz"} <= paths
    assert m["national_build"]["scaling_zones"]["zones"] == ["Bay of Plenty", "Hawke's Bay near", "Hawke's Bay far"]
    # the job reads them from its inputs
    job = parse_config(dict(CFG, paths={"nz_data": str(tmp_path / "in")}, scaling=cfg_scaling(cfg)))
    got = Z.load_zones(job, log=lambda m: None)
    assert got.names == zm.names and np.array_equal(got.zone_of, zm.zone_of)
    hb = {k: int(got.zone_of[ROW[k]]) for k in ("G", "F", "I", "H")}                # Hawke's Bay: G, F 5 km away
    assert hb == {"G": 1, "F": 1, "I": 2, "H": 2} and (got.zone_of[REGION_OF == 0] == 0).all()


def cfg_scaling(cfg):
    sc = cfg.scaling
    return {"zones": [{"region": s.region, "divide_km": s.divide_km, "near": s.near, "far": s.far} for s in sc.zones],
            "divide": {"west": list(sc.divide_west), "east": list(sc.divide_east)}}


def test_ecmwf_unavailable_is_exit_75_other_errors_are_not(tmp_path, monkeypatch):
    data, _, w = _national(tmp_path)
    cfg = parse_config(dict(CFG, paths={"nz_data": str(data)}))
    with pytest.raises(pl.SourceUnavailable, match="no nothing run"):
        pl.run_ecmwf(cfg, reader=_Nothing(ECMWF), log=lambda m: None)
    calls = []

    def fake(cfg, **kw):
        calls.append(kw["http"])
        raise FetchError("https://openmeteo/...: HTTP 503 after 8 attempts")

    monkeypatch.setattr(pl, "run_ecmwf", fake)
    with pytest.raises(SystemExit) as e:
        cli.main(["rain", "ecmwf", "--data", str(data), "--retries", "7", "--no-cache"])
    assert e.value.code == pl.EXIT_UNAVAILABLE == 75 and calls[0].retries == 7

    def boom(cfg, **kw):
        raise ValueError("source grid toy != weights grid ecmwf")

    monkeypatch.setattr(pl, "run_ecmwf", boom)
    with pytest.raises(ValueError):
        cli.main(["rain", "ecmwf", "--data", str(data)])


def test_observed_window_never_ends_after_now():
    class Early(Reader):
        name = "early"

        def _list_leads(self, run):
            return tuple(range(1, 91))

        def hourly(self, run, lead):
            return np.zeros(TOY.shape, np.float32), "zero"

    run = datetime(2026, 11, 20, 0, tzinfo=UTC)
    now_h = hour_index(run + timedelta(hours=5))           # 05:50Z: lead 6 (06:00Z) is already out
    plan, missing, end = obs_plan(Early(TOY), run, (1, 6), 5)
    assert end == now_h + 1
    plan, missing, end = obs_plan(Early(TOY), run, (1, 6), 5, now_hour=now_h)
    assert end == now_h and max(plan) == now_h and len(plan) == 24 and not missing
    assert plan[now_h] == (run, 5) and plan[now_h - 23] == (run - timedelta(hours=24), 6)

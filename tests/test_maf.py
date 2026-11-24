import numpy as np
import pandas as pd
import pytest

from river_finder import maf
from river_finder.maf import MafConfig, correct, envelope, fit_envelope


@pytest.fixture(autouse=True)
def _no_override():
    maf.set_override(None)
    yield
    maf.set_override(None)


def _river():
    """A main stem 0 -> 1 -> 2 -> 3 -> 4 (outlet) with a tributary 5 -> 2.

    Reaches 0, 1 and 4 are matched to REC1 at 2 m3/s/km2 * (A/10)^-0.25; 2 and 3 are not matched and carry a
    tile-median specific MAF of 8 m3/s/km2 (the bug: far too much for 200-400 km2)."""
    area = np.array([10.0, 20.0, 200.0, 400.0, 800.0, 150.0])
    down = np.array([1, 2, 3, 4, -1, 2])
    spec = 2.0 * (area / 10.0) ** -0.25
    matched = np.array([True, True, False, False, True, True])
    spec[~matched] = 8.0
    return area, spec * area, matched, down


def test_envelope_and_fit():
    a = np.array([0.5, 2.0, 12.2, 445.0, 3830.0])
    q = np.array([20.0, 21.0, 166.5, 2639.5, 3804.5])
    c = fit_envelope(a, q, b=0.25, min_area_km2=1.0)
    assert c == pytest.approx(2639.5 / 445.0 * 445.0 ** 0.25)          # Whataroa-like gauge sets it
    assert np.all(q[a >= 1] / a[a >= 1] <= envelope(a[a >= 1], c, 0.25) + 1e-9)
    assert envelope(16.0, 10.0, 0.25) == pytest.approx(5.0)
    with pytest.raises(ValueError):
        fit_envelope([0.1], [1.0])


def test_refill_takes_network_neighbours_not_the_tile_median():
    area, q, matched, down = _river()
    r = correct(area, q, matched, down, MafConfig(cap=False))
    want = 2.0 * (area / 10.0) ** -0.25
    # the unmatched reaches sit between matched ones that follow the same A^-0.25 law: refill recovers it
    assert np.allclose(r.specific[[2, 3]], want[[2, 3]], rtol=1e-6)
    assert list(r.flag) == ["", "", "refill", "refill", "", ""]
    assert np.allclose(r.maf[matched], q[matched])


def test_refill_with_one_neighbour_and_without():
    area = np.array([10.0, 40.0, 30.0])
    down = np.array([1, -1, -1])
    q = np.array([30.0, 400.0, 300.0])          # 0 matched; 1 below it unmatched; 2 isolated and unmatched
    matched = np.array([True, False, False])
    r = correct(area, q, matched, down, MafConfig(cap=False))
    assert r.specific[1] == pytest.approx(3.0 * 4.0 ** -0.25)
    assert r.flag[2] == "" and r.maf[2] == pytest.approx(300.0)  # nothing to borrow from: left as it was


def test_cap_at_the_envelope_and_flags():
    area = np.array([2.0, 50.0])
    q = np.array([2.0 * 45.0, 50.0 * 4.8])      # a Taranaki-like 45 m3/s/km2 and a Waiokumurau-like 4.8
    r = correct(area, q, None, None, MafConfig())
    assert r.specific[0] == pytest.approx(27.2 * 2.0 ** -0.25)
    assert r.flag[0] == "cap" and r.flag[1] == ""
    assert r.maf[1] == pytest.approx(240.0)


def test_refilled_value_is_capped_too():
    area = np.array([2.0, 4.0])
    down = np.array([1, -1])
    q = np.array([2.0 * 40.0, 4.0 * 1.0])
    matched = np.array([True, False])
    r = correct(area, q, matched, down, MafConfig(envelope_margin=1.0))
    # the source is capped before it is borrowed, so the refill stays under the envelope
    assert r.flag[0] == "cap" and r.flag[1] == "refill"
    assert r.specific[1] <= envelope(4.0)[()] + 1e-9


def test_drop_flag_and_monotone():
    area = np.array([10.0, 12.0, 30.0])
    down = np.array([1, 2, -1])
    q = np.array([100.0, 50.0, 60.0])           # MAF halves from reach 0 to reach 1
    r = correct(area, q, None, down, MafConfig(cap=False))
    assert list(r.flag) == ["drop", "", ""] and r.maf[0] == pytest.approx(100.0)
    r = correct(area, q, None, down, MafConfig(cap=False, monotone=True))
    assert r.flag[0] == "drop+monotone" and r.maf[0] == pytest.approx(50.0)
    assert r.bits["monotone"].sum() == 1


def test_zero_maf_on_a_matched_reach_is_refilled():
    area = np.array([10.0, 20.0])
    down = np.array([1, -1])
    q = np.array([0.0, 40.0])
    r = correct(area, q, np.array([True, True]), down, MafConfig(cap=False))
    assert r.flag[0] == "refill" and r.specific[0] == pytest.approx(2.0 * 2.0 ** 0.25)


def test_apply_is_idempotent_reverts_and_recomputes_power():
    area, q, matched, down = _river()
    f = pd.DataFrame({"area_km2": area, "maf_m3s": np.round(q, 2), "specific_maf_matched": matched,
                      "down_seg": down, "grad_s": 20.0, "power_kw_m": 0.0, "specific_maf": q / area})
    raw = f["maf_m3s"].copy()
    maf.apply(f, MafConfig(), down="down_seg")
    first = f["maf_m3s"].copy()
    assert (f["maf_flag"].astype(str) == ["", "", "refill", "refill", "", ""]).all()
    assert np.allclose(f["power_kw_m"], 1000 * 9.81 * f["maf_m3s"] * 20.0 / 1e6)
    maf.apply(f, MafConfig(), down="down_seg")        # again: starts from maf_raw_m3s, same answer
    assert np.allclose(f["maf_m3s"], first)
    assert np.allclose(f["maf_raw_m3s"], raw)
    s = maf.summary(f, min_area_km2=0.0)
    assert s["refill"] == 2 and s["cap"] == 0 and s["changed"] == 2
    maf.revert(f)
    assert "maf_flag" not in f and "maf_raw_m3s" not in f
    assert np.allclose(f["maf_m3s"], raw)
    assert np.allclose(f["specific_maf"], raw / area, atol=1e-4)


def test_config_file_and_override(tmp_path):
    cfg = maf.load_config()                             # the committed config/maf.toml (on since 2026-11-24)
    assert cfg.apply is True and cfg.refill and cfg.cap and not cfg.monotone
    assert cfg.envelope_c == pytest.approx(27.2) and cfg.envelope_b == pytest.approx(0.25)
    maf.set_override(False)
    assert maf.load_config().apply is False
    maf.set_override(True)
    assert maf.load_config().apply is True
    maf.set_override(None)
    assert MafConfig().apply is False                   # the code default stays off
    p = tmp_path / "maf.toml"
    p.write_text("[correction]\napply = true\nenvelope_c = 20.0\n")
    assert maf.load_config(p) == MafConfig(apply=True, envelope_c=20.0)
    p.write_text("[correction]\nbogus = 1\n")
    with pytest.raises(ValueError):
        maf.load_config(p)
    with pytest.raises(ValueError):
        MafConfig(envelope_b=1.5)
    assert maf.load_config(tmp_path / "missing.toml") == MafConfig()


def test_prepare_applies_the_correction_only_when_on():
    from river_finder.scoring import prepare

    area, q, matched, down = _river()
    g = pd.DataFrame({"seg_id": range(6), "down_seg": down, "area_km2": area, "maf_m3s": np.round(q, 2),
                      "specific_maf_matched": matched, "gradient_m_per_km": 20.0, "ksn": 5.0, "length_m": 250.0})
    off = prepare(g, MafConfig(apply=False))
    assert "maf_flag" not in off and np.allclose(off["maf_m3s"], g["maf_m3s"])
    on = prepare(g, MafConfig(apply=True))
    assert (on["maf_flag"].astype(str) != "").sum() == 2
    assert on["maf_m3s"].iloc[3] < 0.5 * g["maf_m3s"].iloc[3]
    assert np.allclose(on["power_kw_m"], 1000 * 9.81 * on["maf_m3s"] * on["grad_s"] / 1e6)


def test_corrected_frame_round_trips_through_gpkg(tmp_path):
    import geopandas as gpd
    from shapely.geometry import LineString

    area, q, matched, down = _river()
    g = gpd.GeoDataFrame({"down_seg": down, "area_km2": area, "maf_m3s": np.round(q, 2),
                          "specific_maf_matched": matched},
                         geometry=[LineString([(i, 0), (i + 1, 0)]) for i in range(6)], crs="EPSG:2193")
    maf.apply(g, MafConfig(), down="down_seg")
    g.to_file(tmp_path / "s.gpkg", layer="segments", driver="GPKG")
    back = gpd.read_file(tmp_path / "s.gpkg")
    assert back["maf_flag"].fillna("").tolist() == ["", "", "refill", "refill", "", ""]
    assert np.allclose(back["maf_raw_m3s"], np.round(q, 2))


def test_cli_switch_sets_the_override(monkeypatch):
    from river_finder import cli

    seen = {}
    monkeypatch.setattr(cli, "_ml", lambda args: seen.setdefault("apply", maf.load_config().apply))
    cli.main(["ml", "page", "--maf-correction", "on"])
    assert seen["apply"] is True


def test_sync_maf_applies_reverts_and_reclassifies():
    from river_finder import ml
    from river_finder import reachtype as RT

    area, q, matched, down = _river()
    t = pd.DataFrame({"gid": range(6), "down": down, "area_km2": area, "maf_m3s": np.round(q, 2),
                      "specific_maf_matched": matched, "grad_s": 3.0, "power_kw_m": 0.0,
                      "valley_width_m": 120.0})
    t = pd.concat([t, RT.classify_frame(t)], axis=1)
    raw_conf = t["confinement_ratio"].copy()
    ml.sync_maf(t, MafConfig(apply=True), log=lambda m: None)
    assert (t["maf_flag"].astype(str) == "refill").sum() == 2
    # smaller floods -> narrower bankfull estimate -> a larger valley/bankfull ratio on the refilled reaches
    assert (t["confinement_ratio"].iloc[[2, 3]] > raw_conf.iloc[[2, 3]]).all()
    ml.sync_maf(t, MafConfig(apply=False), log=lambda m: None)
    assert "maf_flag" not in t
    assert np.allclose(t["confinement_ratio"], raw_conf)
    untouched = t.copy()
    ml.sync_maf(t, MafConfig(apply=False), log=lambda m: None)   # off and never corrected: no-op
    pd.testing.assert_frame_equal(t, untouched)

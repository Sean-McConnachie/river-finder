"""Hydro-affected reaches (hydro.py) on a synthetic network."""

import numpy as np
import pandas as pd
import pytest

from river_finder import hydro as H
from river_finder.models import _TO_LL, _finish

X0, Y0 = 1_800_000.0, 5_800_000.0


def _frame() -> pd.DataFrame:
    """Alpha River: rows 0-9 west to east, 250 m each. Beta Stream (10-12) joins at row 4 from the south; an unnamed
    side creek (13) joins row 1 from 100 m south of it."""
    rows = []
    for i in range(10):
        area = [10, 11, 12, 13, 33, 34, 35, 36, 37, 38][i]
        rows.append(("Alpha River", X0 + 250 * i, Y0, area, i + 1 if i < 9 else -1, 5.0))
    for k, y in enumerate((Y0 - 750, Y0 - 500, Y0 - 250)):
        rows.append(("Beta Stream", X0 + 1000, y, 18 + k, 11 + k if k < 2 else 4, 8.0))
    rows.append(("", X0 + 250, Y0 - 100, 1.5, 1, 30.0))
    f = pd.DataFrame(rows, columns=["river", "x", "y", "area_km2", "down", "grad_s"])
    f["length_m"] = 250.0
    f["seg_id"] = np.arange(len(f))
    return _finish(f, f.pop("x").to_numpy(), f.pop("y").to_numpy())


def _ll(x, y):
    lon, lat = _TO_LL.transform(x, y)
    return float(lon), float(lat)


def _cfg(points: list[dict], **settings) -> H.HydroConfig:
    return H.parse_config({"settings": {"dam_up_m": 0.0, **settings},
                           "scheme": [{"id": "s", "name": "Test scheme", "sources": ["https://example.org"],
                                       "point": points}]})


def _pt(pid, river, x, y, role="intake", **kw):
    lon, lat = _ll(x, y)
    return {"id": pid, "river": river, "lon": lon, "lat": lat, "role": role, **kw}


def test_intake_snaps_to_the_named_river_not_the_nearest_reach():
    f = _frame()
    cfg = _cfg([_pt("weir", "Alpha River", X0 + 250, Y0 - 80)])     # 20 m from the side creek, 80 m from Alpha
    (s,) = H.snap(f, cfg)
    assert s.row == 1 and s.named and s.river == "Alpha River"
    (s,) = H.snap(f, _cfg([_pt("weir", "", X0 + 250, Y0 - 80)]))  # no name: the nearest reach of 1 km2 or more
    assert s.row == 13 and not s.named


def test_diverted_until_the_tailrace_then_regulated():
    f = _frame()
    weir = _pt("weir", "Alpha River", X0 + 250, Y0)                   # row 1, 11 km2
    h = H.classify(f, _cfg([weir]))
    assert h["hydro"][:4].tolist() == ["", "diverted", "diverted", "diverted"]   # row 3: 11 of 13 km2 taken
    assert h["hydro"][4] == ""                # Beta joins at row 4: 11 of 33 km2, below the 0.5 share
    h = H.classify(f, _cfg([weir, _pt("tailrace", "Alpha River", X0 + 750, Y0, role="return")]))   # row 3
    assert h["hydro"][:5].tolist() == ["", "diverted", "diverted", "regulated", ""]
    assert h["hydro_point"][1] == "weir" and h["hydro_point"][3] == "tailrace"
    h = H.classify(f, _cfg([weir, _pt("tailrace", "Alpha River", X0 + 750, Y0, role="return", regulates=False)]))
    assert h["hydro"][:5].tolist() == ["", "diverted", "diverted", "", ""]   # run of river: natural again below
    assert (h["hydro_scheme"][h["hydro"] != ""] == "s").all()


def test_diverted_stops_at_the_named_confluence():
    f = _frame()
    beta = _pt("beta_intake", "Beta Stream", X0 + 1000, Y0 - 750)    # row 10, 18 km2
    h = H.classify(f, _cfg([beta]))
    assert h["hydro"][[10, 11, 12]].tolist() == ["diverted"] * 3
    assert h["hydro"][4] == "diverted"        # 18 of 33 km2: still over half the natural flow
    h = H.classify(f, _cfg([dict(beta, until="Alpha River")]))
    assert h["hydro"][[10, 11, 12]].tolist() == ["diverted"] * 3
    assert (h["hydro"][:10] == "").all()      # the walk ends where Beta enters the Alpha
    h = H.classify(f, _cfg([dict(beta, km=0.5)]))
    assert h["hydro"][[10, 11, 12]].tolist() == ["diverted", "diverted", ""]


def test_several_intakes_add_up_and_nested_ones_count_once():
    f = _frame()
    a = _pt("alpha_weir", "Alpha River", X0, Y0)                      # row 0, 10 km2
    b = _pt("beta_weir", "Beta Stream", X0 + 1000, Y0 - 750, take=0.4)   # row 10: 0.4 * 18 = 7.2 km2
    h = H.classify(f, _cfg([a, b]))
    assert h["hydro"][4] == "diverted"        # (10 + 7.2) / 33 = 0.52
    assert h["hydro"][6] == ""                # 17.2 / 35 < 0.5
    assert h["hydro"][10] == "" and h["div_share"][10] == pytest.approx(0.4)
    # a second weir below the first takes only what is left in the river: no double counting
    h = H.classify(f, _cfg([a, _pt("alpha_weir2", "Alpha River", X0 + 500, Y0)]))
    assert h["div_share"][3] == pytest.approx(12 / 13, abs=1e-3)   # 10 km2 above the first weir + the 2 between
    assert h["div_share"][4] == pytest.approx(12 / 33, abs=1e-3)


def test_reservoir_is_the_flat_reaches_above_the_dam_and_the_dam():
    f = _frame()
    f.loc[[5, 6], "grad_s"] = 0.0                                     # the lake surface
    f.loc[4, "grad_s"] = 20.0
    dam = _pt("dam", "Alpha River", X0 + 1750, Y0, role="dam", reservoir=True)   # row 7
    h = H.classify(f, _cfg([dam], reservoir_grad=0.5, dam_m=300.0))
    assert set(np.flatnonzero((h["hydro"] == "reservoir").to_numpy())) == {5, 6, 7, 8}   # 8: dam face, 250 m below
    assert h["hydro"][9] == "regulated"       # below the dam
    assert h["hydro"][4] == ""                # the river above the lake
    assert h["hydro_share"][6] == 1.0


def test_config_parses_and_rejects_bad_points(tmp_path):
    cfg = H.load_config()
    assert cfg.schemes and {"diverted", "reservoir"} <= set(cfg.audit_exclude)
    ids = [s.id for s in cfg.schemes]
    assert len(ids) == len(set(ids))
    assert all(s.sources for s in cfg.schemes)
    assert all(p.role in H.ROLES for p in cfg.points())
    with pytest.raises(ValueError):
        _cfg([{"id": "x", "lon": 175.0, "lat": -39.0, "role": "spillway"}])
    with pytest.raises(ValueError):
        _cfg([{"id": "x", "lon": 175.0, "lat": -39.0, "role": "return", "returns": ["nope"]}])
    assert H.load_config(tmp_path / "missing.toml").schemes == []


def test_run_class_needs_half_the_run():
    h = pd.DataFrame({"hydro": ["diverted", "diverted", "", "regulated"], "hydro_scheme": ["a", "a", "", "b"]})
    length = np.array([100.0, 100.0, 100.0, 100.0])
    assert H.run_class(h, [0, 1, 2], length) == ("diverted", "a", 0.67)
    assert H.run_class(h, [2, 3], length) == ("regulated", "b", 0.5)
    assert H.run_class(h, [0, 2, 2], length)[0] == ""


def test_web_record_lists_flagged_viewer_reaches():
    from river_finder.webdata import hydro_record

    f = _frame()
    cfg = _cfg([_pt("weir", "Alpha River", X0 + 250, Y0)])
    h = H.classify(f, cfg)
    vi = np.arange(len(f))
    vi[2] = -1                                 # not shown in the viewer
    rec = hydro_record(h, vi, cfg)
    assert rec["classes"][1] == "diverted" and rec["schemes"][0]["name"] == "Test scheme"
    assert [r[0] for r in rec["r"]] == [1, 3]
    assert all(r[1] == 1 and r[2] == 0 for r in rec["r"]) and rec["r"][0][3] == 100

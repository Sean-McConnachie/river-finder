import json

import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer

from river_finder import models as M
from river_finder.labels import Section
from river_finder.research import guide_region, load_research, parse_point, sourced_only

_LL = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)


def test_parse_point_shapes():
    assert parse_point([-37.8, 176.0]) == (176.0, -37.8)          # [lat, lon]
    assert parse_point([176.0, -37.8]) == (176.0, -37.8)          # [lon, lat]
    assert parse_point({"lat": -37.8, "lng": 176.0}) == (176.0, -37.8)
    assert parse_point("-37.8, 176.0") == (176.0, -37.8)
    assert parse_point([0, 0]) is None


def test_guide_region_aliases():
    assert guide_region("Manawatū-Whanganui") == "Manawatu"
    assert guide_region("Nelson") == "Tasman"
    assert guide_region("Hawke's Bay") == "Hawke's Bay"
    assert guide_region("Upper Waikato") == "Waikato"
    assert guide_region("Atlantis") is None


def test_load_research_real_layout(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"metadata": {}, "sections": [
        {"id": "a", "river": "Test River", "section": "Gorge", "region": "Bay of Plenty", "grade": "4",
         "length_km": 3.0, "put_in_coords": [-37.8, 176.0], "inferred_put_in_coords": False,
         "take_out_coords": [-37.82, 176.02], "inferred_take_out_coords": True},
        {"id": "b", "river": "Other Stream", "region": "West Coast", "grade": "3",
         "put_in_coords": [-42.0, 171.5], "inferred_put_in_coords": False,
         "take_out_coords": [-42.05, 171.55], "inferred_take_out_coords": False},
        {"id": "c", "river": "Third Creek", "region": "Otago", "grade": "2",
         "put_in_coords": [-45.0, 169.0], "inferred_put_in_coords": True,
         "take_out_coords": [-45.1, 169.1], "inferred_take_out_coords": True},
    ]}))
    a, b, c = load_research(p)
    assert (a.coords, b.coords, c.coords) == ("inferred", "sourced", "inferred")
    assert a.put_in == (176.0, -37.8) and a.extra["length_km"] == 3.0 and a.grade == "4"
    pa, pb, pc = (sourced_only(s) for s in (a, b, c))
    assert pa.take_out is None and pa.put_in == a.put_in and pa.extra["extent_from"] == "published put-in + length"
    assert pb.take_out == b.take_out
    assert pc is None   # nothing published: dropped from the published-only variant


def _frame():
    """Two rivers draining east: 'Alpha River' (rows 0-5) and 'Beta Stream' (rows 6-9)."""
    x0, y0 = 1_850_000.0, 5_800_000.0
    xs = [x0 + 250 * i for i in range(6)] + [x0 + 250 * i for i in range(4)]
    ys = [y0] * 6 + [y0 + 20_000] * 4
    down = [1, 2, 3, 4, 5, -1, 7, 8, 9, -1]
    f = pd.DataFrame({"river": ["Alpha River"] * 6 + ["Beta Stream"] * 4,
                      "area_km2": [3, 4, 5, 6, 7, 8, 3, 4, 5, 6.0], "length_m": 250.0,
                      "grad_s": [30.0] * 10, "down": down, "guide_region": "Waikato"})
    f = M._finish(f, np.array(xs), np.array(ys))
    return f, xs, ys


def test_map_section_put_in_to_take_out_and_name():
    f, xs, ys = _frame()
    s = Section("Waikato", "Alpha", "runnable", put_in=_LL.transform(xs[1], ys[1] + 50),
                take_out=_LL.transform(xs[4], ys[4] - 50))
    rows, how = M.map_section(f, s, M._Index(f))
    assert list(rows) == [1, 2, 3, 4] and how == "put-in to take-out"
    rows, how = M.map_section(f, Section("Waikato", "Beta Stream", "runnable"), M._Index(f))
    assert list(rows) == [6, 7, 8, 9] and how == "name"


def test_split_keeps_rivers_whole():
    f, xs, ys = _frame()
    secs = [Section("Waikato", "Alpha River", "runnable", put_in=_LL.transform(xs[0], ys[0]),
                    take_out=_LL.transform(xs[2], ys[2])),
            Section("Waikato", "Alpha", "runnable", put_in=_LL.transform(xs[3], ys[3]),
                    take_out=_LL.transform(xs[5], ys[5])),
            Section("Waikato", "Beta Stream", "runnable")]
    mapped = M.map_sections(f, secs)
    assert mapped[0].key == mapped[1].key            # two sections, one river
    for seed in range(20):
        sp = M.make_split(mapped, "t", seed=seed, test_frac=0.5)
        assert not set(sp["train"]) & set(sp["test"])
        assert len(sp["train"]) + len(sp["test"]) == 2


def test_train_leaves_test_rivers_out(monkeypatch):
    f, xs, ys = _frame()
    for c in M._RAW:
        if c not in f:
            f[c] = np.linspace(1, 2, len(f))
    f["maf_m3s"] = np.linspace(1, 20, len(f))
    secs = [Section("Waikato", "Alpha River", "runnable"), Section("Waikato", "Beta Stream", "runnable")]
    mapped = M.map_sections(f, secs)
    split = {"name": "t", "train": [mapped[0].key], "test": [mapped[1].key]}
    monkeypatch.setattr(M, "list_cards", lambda: [])
    card = M.train(f, mapped, split, "vt", "t", "")
    assert card["labels"]["train_rivers"] == [mapped[0].key]
    assert card["labels"]["training_reaches"] == 6      # Beta's four reaches never enter training
    assert len(card["pool_quantiles"]) == 1001


def test_cards_are_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MODELS_DIR", tmp_path)
    card = {"id": "vx", "coef": {"a": 1.0}, "intercept": 0.0}
    M.save_card(card)
    M.save_card(dict(card))   # same weights: fine
    with pytest.raises(FileExistsError):
        M.save_card({**card, "coef": {"a": 2.0}})

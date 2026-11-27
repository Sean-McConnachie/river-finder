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


def test_section_labels_research_grades_and_regions():
    """The packed sta/gr/lho (viewer_nz.national_labels): every known section, research included, with its grade;
    the highest grade and not-runnable win; a section across a region edge labels both regions (rf-7km)."""
    from river_finder.viewer_nz import national_labels

    f, xs, ys = _frame()
    f["region"] = ["Waikato"] * 3 + ["Bay of Plenty"] * 3 + ["Waikato"] * 4
    f["seg_id"] = [0, 1, 2, 0, 1, 2, 3, 4, 5, 6]
    pt = lambda i, dy=0: _LL.transform(xs[i], ys[i] + dy)  # noqa: E731
    research = {"id": "rg-1", "section": "Gorge", "primary_source": "NZ RiverGuide"}
    secs = [Section("Waikato", "Alpha River", "runnable", grade="4-4+", put_in=pt(1, 50), take_out=pt(4, -50),
                    extra=dict(research)),                                   # rows 1-4, across the region edge
            Section("Waikato", "Alpha River", "not_runnable", put_in=pt(3, 50), take_out=pt(4, -50)),
            Section("Waikato", "Beta Stream", "runnable", grade="III", extra={"id": "rg-2"}),   # name rule: 6-9
            Section("Waikato", "Beta Stream", "runnable", grade="2", held_out=True),
            Section("Waikato", "Nowhere Creek", "runnable", grade="5")]       # not placed
    status, grade, held, placed = M.section_labels(f, secs)
    assert status.tolist() == [0, 1, 1, 2, 2, 0, 1, 1, 1, 1]
    assert np.allclose(grade[1:5], 4 + 1 / 6) and np.isnan(grade[[0, 5]]).all() and (grade[6:] == 3).all()
    assert held.tolist() == [False] * 6 + [True] * 4
    assert [m.section.river for m in placed] == ["Alpha River", "Alpha River", "Beta Stream", "Beta Stream"]
    L = national_labels(f, secs)
    assert set(L) == {"Waikato", "Bay of Plenty"}
    w, b = L["Waikato"], L["Bay of Plenty"]
    assert w.status.tolist() == [0, 1, 1, 1, 1, 1, 1] and w.seg_id.tolist() == [0, 1, 2, 3, 4, 5, 6]
    assert b.status.tolist() == [2, 2, 0] and np.allclose(b.grade[:2], 4 + 1 / 6) and np.isnan(b.grade[2])
    assert [s.extra.get("id") for s in w.sections] == ["rg-1", "rg-2", None]
    assert len(b.sections) == 2   # the research gorge and the not-runnable piece both reach Bay of Plenty


def test_map_section_snap_river_override(tmp_path):
    """'Alpha River' (rows 0-3) drains into 'Main River' (rows 4-7); an unnamed creek (rows 8-9) joins row 2.
    A take-out on the main river snaps to the nearest Alpha reach unless ``take_out_river`` names the main river;
    a put-in on the unnamed creek snaps to it only with ``put_in_river`` = ""."""
    x0, y0 = 1_850_000.0, 5_800_000.0
    xs = [x0 + 250 * i for i in range(8)] + [x0 + 500, x0 + 500]
    ys = [y0] * 8 + [y0 + 500, y0 + 250]
    f = pd.DataFrame({"river": ["Alpha River"] * 4 + ["Main River"] * 4 + ["", ""],
                      "area_km2": [3, 4, 5, 6, 20, 21, 22, 23, 1.5, 2.0], "length_m": 250.0, "grad_s": 30.0,
                      "down": [1, 2, 3, 4, 5, 6, 7, -1, 9, 2], "guide_region": "Waikato"})
    f = M._finish(f, np.array(xs), np.array(ys))
    idx = M._Index(f)
    pi, to = _LL.transform(xs[1], ys[1] + 30), _LL.transform(xs[6], ys[6] + 30)
    rows, how = M.map_section(f, Section("Waikato", "Alpha River", "runnable", put_in=pi, take_out=to), idx)
    assert list(rows) == [1, 2, 3] and how == "put-in to take-out"     # take-out snapped back onto the Alpha
    s = Section("Waikato", "Alpha River", "runnable", put_in=pi, take_out=to, extra={"take_out_river": "Main River"})
    assert M.snap_river(s, "take_out") == "Main River" and M.snap_river(s, "put_in") == "Alpha River"
    rows, how = M.map_section(f, s, idx)
    assert list(rows) == [1, 2, 3, 4, 5, 6] and how == "put-in to take-out"
    creek = _LL.transform(xs[8], ys[8])
    s = Section("Waikato", "Alpha River", "runnable", put_in=creek, take_out=_LL.transform(xs[3], ys[3]))
    assert list(M.map_section(f, s, idx)[0]) == [2, 3]                 # put-in snapped to the nearest Alpha reach
    s.extra = {"put_in_river": ""}
    assert list(M.map_section(f, s, idx)[0]) == [8, 9, 2, 3]           # '' = the nearest reach, whatever its name
    # the research importer keeps the override fields
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"sections": [{"id": "x", "river": "Alpha River", "region": "Waikato",
                                           "put_in_coords": [-37.8, 176.0], "take_out_coords": [-37.82, 176.02],
                                           "take_out_river": "Main River", "put_in_river": ""}]}))
    (r,) = load_research(p)
    assert M.snap_river(r, "take_out") == "Main River" and M.snap_river(r, "put_in") == ""


def test_map_section_name_rule_keeps_one_valley():
    """'Alpha River' in one valley (rows 0-5, row 2 too flat) and 'Alpha Stream' (same name once normalised)
    in another valley 5 km north (rows 6-8): the name rule keeps one of them."""
    x0, y0 = 1_850_000.0, 5_800_000.0
    xs = [x0 + 250 * i for i in range(6)] + [x0 + 250 * i for i in range(3)]
    ys = [y0] * 6 + [y0 + 5_000] * 3
    f = pd.DataFrame({"river": ["Alpha River"] * 6 + ["Alpha Stream"] * 3,
                      "area_km2": [3, 4, 5, 6, 7, 8, 3, 4, 5.0], "length_m": 250.0,
                      "grad_s": [30, 30, 5, 30, 30, 30, 30, 30, 30.0], "down": [1, 2, 3, 4, 5, -1, 7, 8, -1],
                      "guide_region": "Waikato"})
    f = M._finish(f, np.array(xs), np.array(ys))
    idx = M._Index(f)
    info = {}
    rows, how = M.map_section(f, Section("Waikato", "Alpha River", "runnable"), idx, info)
    assert list(rows) == [0, 1, 3, 4, 5] and how == "name (1 of 2 pieces)"
    assert info["pieces"] == 2 and info["dropped_reaches"] == 3 and list(info["gap_rows"]) == [2]
    s = Section("Waikato", "Alpha River", "runnable", take_out=_LL.transform(xs[7], ys[7] + 100))
    rows, how = M.map_section(f, s, idx)
    assert list(rows) == [6, 7, 8] and how == "name near the coordinates (path did not join; 1 of 2 pieces)"
    m = M.map_sections(f, [Section("Waikato", "Alpha River", "runnable")])[0]
    assert m.method == "name (1 of 2 pieces)" and m.info["pieces"] == 2

    from river_finder.webdata import section_record

    rec = section_record(m.section, [0, 1, 3, 4, 5], m.method, info=m.info, gap=[2])
    assert rec["dropped"] == {"pieces": 1, "branches": 0, "km": 0.75} and rec["gap"] == [2]
    assert section_record(m.section, [1], "put-in to take-out")["dropped"] is None


def test_norm_river_forks_and_aliases():
    # the kind of water, macrons and parentheses still do not matter; fork words go to river_side
    assert M.norm_river("Kaitūna R.") == M.norm_river("Kaituna Stream") == "kaituna"
    assert M.norm_river("Matukituki River East Branch") == M.norm_river("Matukituki River (West)") == "matukituki"
    assert M.river_side("Matukituki River East Branch") == M.river_side("Matukituki River (East)") == "east"
    assert M.river_side("North Branch Temple Stream") == "north"
    assert M.river_side("North East Gorge Stream") == "east north"
    assert M.river_side("Kaituna River") == "" and M.river_side("Westland Creek") == ""
    # a name made only of such words keeps them (it was '', i.e. unnamed)
    assert M.norm_river("Branch River") == M.norm_river("Branch Stream") == "branch"
    assert M.norm_river("North River") == "north" and M.norm_river("Burn Creek") == "burn creek"
    # guide spellings REC2 writes differently
    assert M.norm_river("Routeburn River") == M.norm_river("Route Burn")
    assert M.norm_river("Tauwharenikau River") == M.norm_river("Tauherenikau River")
    assert M.norm_river("Hangatuha (Stony River)") == M.norm_river("Stony River")
    assert list(M.same_side("south", ["", "south", "north", "north south"])) == [True, True, False, False]
    assert M.same_side("", ["north", "south"]).all()


def _forks():
    """'Temple Stream' (rows 0-2, draining east) fed by 'North Branch Temple Stream' (rows 3-4, from the north) and
    'Temple Stream South Branch' (rows 5-6, from the south)."""
    x0, y0 = 1_850_000.0, 5_800_000.0
    xs = [x0, x0 + 250, x0 + 500, x0 - 500, x0 - 250, x0 - 500, x0 - 250]
    ys = [y0, y0, y0, y0 + 1000, y0 + 500, y0 - 1000, y0 - 500]
    f = pd.DataFrame({"river": ["Temple Stream"] * 3 + ["North Branch Temple Stream"] * 2
                      + ["Temple Stream South Branch"] * 2,
                      "area_km2": [10, 11, 12, 3, 4, 3.5, 4.5], "length_m": 250.0, "grad_s": 30.0,
                      "down": [1, 2, -1, 4, 0, 6, 0], "guide_region": "Waikato"})
    return M._finish(f, np.array(xs), np.array(ys)), xs, ys


def test_map_section_keeps_the_named_fork():
    f, xs, ys = _forks()
    idx = M._Index(f)
    # a put-in placed by the north fork: 'South Temple Stream' never snaps to the north fork (the main stem is nearer
    # than the south fork); the old match took the north fork
    pi, to = _LL.transform(xs[3], ys[3] - 100), _LL.transform(xs[2], ys[2])
    rows, how = M.map_section(f, Section("Waikato", "South Temple Stream", "runnable", put_in=pi, take_out=to), idx)
    assert list(rows) == [0, 1, 2] and how == "put-in to take-out"
    rows, how = M.map_section(f, Section("Waikato", "Temple Stream", "runnable", put_in=pi, take_out=to), idx)
    assert list(rows) == [3, 4, 0, 1, 2]                                  # no fork named: any fork, as before
    # the name rule: the named fork and the main stem, or one fork of the two when none is named
    rows, how = M.map_section(f, Section("Waikato", "Temple Stream (South)", "runnable"), idx)
    assert list(rows) == [0, 1, 2, 5, 6] and how == "name"
    rows, how = M.map_section(f, Section("Waikato", "Temple Stream", "runnable"), idx)
    assert len(rows) == 5 and how == "name (1 of 2 branches)"
    # a fork named in the section title, when the river name has none
    s = Section("Waikato", "Temple Stream", "runnable", extra={"section": "North Branch to the road end"})
    assert M.section_side(s) == "north" and list(M.map_section(f, s, idx)[0]) == [0, 1, 2, 3, 4]
    # a point that snaps to another river uses that name's fork words
    s.extra["take_out_river"] = "Temple Stream South Branch"
    assert M.section_side(s, "take_out") == "south" and M.section_side(s, "put_in") == "north"


def _side_stream(side_area: float):
    """'Alpha River' (rows 0-5, 250 m reaches, then rows 9-18, 1 km reaches, draining east) joined at row 3 by a
    creek from 5 km north (rows 6-8, 1 km reaches)."""
    x0, y0 = 1_850_000.0, 5_800_000.0
    xs = [x0 + 250 * i for i in range(6)] + [x0 + 750] * 3 + [x0 + 2250 + 1000 * i for i in range(10)]
    ys = [y0] * 6 + [y0 + 5000, y0 + 4000, y0 + 3000] + [y0] * 10
    f = pd.DataFrame({"river": ["Alpha River"] * 6 + ["Side Creek"] * 3 + ["Alpha River"] * 10,
                      "area_km2": [10, 11, 12, 13, 14, 15] + [side_area] * 3 + list(range(16, 26)),
                      "length_m": [250.0] * 6 + [1000.0] * 13, "grad_s": 30.0,
                      "down": [1, 2, 3, 4, 5, 9, 7, 8, 3] + list(range(10, 19)) + [-1], "guide_region": "Waikato"})
    return M._finish(f, np.array(xs), np.array(ys)), xs, ys


def test_put_in_on_a_side_stream_starts_at_the_confluence():
    """A put-in 5 km up a side creek (no 'Alpha' reach within 3 km): 'put-in + length' and 'put-in towards take-out'
    start where the creek joins the river, unless the creek is the river's main upstream reach (its own unnamed or
    misnamed upper course) or the label names the put-in's river."""
    f, xs, ys = _side_stream(2.0)
    idx = M._Index(f)
    pi = _LL.transform(xs[6], ys[6] + 50)
    s = Section("Waikato", "Alpha River", "runnable", put_in=pi, extra={"length_km": 0.4})
    info = {}
    rows, how = M.map_section(f, s, idx, info)
    assert list(rows) == [3, 4] and how.startswith("put-in + length (from the confluence")
    assert info["side_km"] == 3.0
    s = Section("Waikato", "Alpha River", "runnable", put_in=pi, take_out=_LL.transform(xs[18], ys[18] - 3100))
    rows, how = M.map_section(f, s, idx)                           # take-out 3.1 km off: no path, walk towards it
    assert list(rows) == [3, 4, 5, *range(9, 19)] and how.startswith("put-in towards take-out (from the confluence")
    s = Section("Waikato", "Alpha River", "runnable", put_in=pi, extra={"length_km": 0.4, "put_in_river": ""})
    assert list(M.map_section(f, s, idx)[0]) == [6]                      # the label says: whatever reach is there
    f, xs, ys = _side_stream(20.0)                                       # the creek is the bigger feeder of row 3
    rows, how = M.map_section(f, Section("Waikato", "Alpha River", "runnable", put_in=pi, extra={"length_km": 0.4}),
                              M._Index(f))
    assert list(rows) == [6] and how == "put-in + length"
    # the guides' Manganui-a-te-ao is REC2's Manganui-o-te-ao River
    assert M.norm_river("Manganui-a-te-ao") == M.norm_river("Manganui-o-te-ao River")


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

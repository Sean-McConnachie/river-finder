"""LINZ river lines for the map: network order, dropping small headwater lines, the compact JSON."""

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString

from river_finder import linz

X0, Y0 = 1_750_000.0, 5_900_000.0  # NZTM


def _ln(*pts):
    return LineString([(X0 + x, Y0 + y) for x, y in pts])


def _network():
    """An unnamed network draining to O = (0, 0), some lines digitised upstream, plus other kinds.

    O -L0- J1, J1 -L1- A (headwater), J1 -L2- J2, J2 -L3- B and J2 -L4- C (headwaters).
    Strahler: L3 = L4 = 1, so L2 = 2; L1 = 1, so L0 = 2.
    """
    rows = [
        ("L0", "river_cl", None, _ln((0, 1000), (0, 0))),            # digitised upstream
        ("L1", "river_cl", None, _ln((0, 1000), (-500, 2000))),
        ("L2", "river_cl", None, _ln((500, 2000), (0, 1000))),       # digitised upstream
        ("L3", "river_cl", None, _ln((300, 3000), (500, 2000))),
        ("L4", "river_cl", None, _ln((500, 2000), (900, 3000))),
        ("N1", "river_cl", "Main Creek", _ln((5000, 0), (5000, 500))),
        ("N2", "river_cl", "Main Creek", _ln((5000, 500), (5100, 1500))),
        ("BIG", "river_cl", None, _ln((8000, 0), (8000, 3000))),     # one long unnamed line, 3 km²
        ("CAN", "canal_cl", None, _ln((10000, 0), (10000, 800))),
        ("DR", "drain_cl", "Big Drain", _ln((12000, 0), (12000, 800))),
    ]
    g = gpd.GeoDataFrame({"river_section_id": range(100, 100 + len(rows)), "feat_type": [r[1] for r in rows],
                          "name": [r[2] for r in rows], "key": [r[0] for r in rows]},
                         geometry=[r[3] for r in rows], crs=2193)
    area = {"L0": 6.0, "L1": 0.4, "L2": 1.5, "L3": 0.2, "L4": 0.3, "N1": 0.1, "N2": 0.2, "BIG": 3.0,
            "CAN": 0.0, "DR": 0.0}
    return g, area


def _area_fn(g, area):
    """line_area for compact(): looks lines up by geometry (compact passes them filtered)."""
    by_wkb = {geom.wkb: area[k] for geom, k in zip(g.geometry, g["key"], strict=True)}
    return lambda geoms: np.array([by_wkb[x.wkb] for x in geoms])


def _decode(d):
    """Lines of the compact JSON back to [[lon, lat], ...] (as 50-rivers.js does)."""
    q = d["q"]
    return [np.cumsum(np.asarray(flat, float).reshape(-1, 2), axis=0) * q for _, _, _, flat in d["lines"]]


def test_stream_order_from_the_outlet_whatever_the_digitising():
    g, area = _network()
    rivers = g[g["key"].str.startswith("L")]
    score = np.array([area[k] for k in rivers["key"]])
    order = dict(zip(rivers["key"], linz.stream_order(rivers.geometry.to_numpy(), score), strict=True))
    assert order == {"L0": 2, "L1": 1, "L2": 2, "L3": 1, "L4": 1}
    # with the outlet taken at headwater A instead, the line to O becomes a twig: the area picks the outlet
    wrong = linz.stream_order(rivers.geometry.to_numpy(), np.where(rivers["key"] == "L1", 99.0, 0.0))
    assert wrong[list(rivers["key"]).index("L0")] == 1


def test_stream_order_braid_loop_takes_the_lower_end():
    lines = [_ln((0, 0), (0, 1000)), _ln((0, 1000), (0, 2000)), _ln((0, 1000), (200, 1500), (0, 2000)),
             _ln((0, 2000), (-300, 3000)), _ln((0, 2000), (300, 3000))]
    order = linz.stream_order(np.array(lines, dtype=object), [5.0, 2.0, 2.0, 0.5, 0.5])
    assert list(order) == [2, 2, 2, 1, 1]


def test_compact_drops_small_unnamed_headwaters_and_joins_what_is_left():
    g, area = _network()
    d = linz.compact(g, line_area=_area_fn(g, area))
    st = d["stats"]
    assert st["headwater_dropped"] == 3 and st["drains"] == 1  # L1, L3, L4; the drain
    assert d["names"] == ["Main Creek"] and d["types"] == list(linz.KEEP_TYPES)
    lines = _decode(d)
    kinds = sorted((d["types"][t], n) for t, n, _, _ in d["lines"])
    # L0 + L2 joined into one line, BIG kept (3 km²), Main Creek merged, the canal kept
    assert kinds == [("canal_cl", -1), ("river_cl", -1), ("river_cl", -1), ("river_cl", 0)]
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    o, j1, j2 = (to_ll.transform(X0 + x, Y0 + y) for x, y in ((0, 0), (0, 1000), (500, 2000)))
    joined = next(c for c, (t, n, _, _) in zip(lines, d["lines"], strict=True) if n < 0 and len(c) == 3)
    ends = {tuple(np.round(joined[0], 4)), tuple(np.round(joined[-1], 4))}
    assert ends == {tuple(np.round(o, 4)), tuple(np.round(j2, 4))}
    assert np.allclose(joined[1], j1, atol=2e-5)
    named = lines[[n for _, n, _, _ in d["lines"]].index(0)]
    assert len(named) == 3  # N1 + N2 merged, each point within q of the original
    want = {tuple(np.round(to_ll.transform(X0 + x, Y0 + y), 4)) for x, y in ((5000, 0), (5100, 1500))}
    assert {tuple(np.round(named[0], 4)), tuple(np.round(named[-1], 4))} == want


def test_compact_without_area_keeps_every_river_line():
    g, _ = _network()
    d = linz.compact(g)
    assert "headwater_dropped" not in d["stats"]
    assert sum(n < 0 for _, n, _, _ in d["lines"]) >= 3  # the unnamed network (joined where only two meet), BIG, canal
    kept = sum(len(c) - 1 for c in _decode(d))
    assert kept == sum(len(x.coords) - 1 for x, t in zip(g.geometry, g["feat_type"], strict=True) if t != "drain_cl")


def test_line_area_km2_reads_the_grid_along_each_line():
    from river_finder.areacode import encode_area

    to_m = Transformer.from_crs("EPSG:2193", "EPSG:3857", always_xy=True)
    x0, y0 = to_m.transform(X0 - 1000, Y0 - 1000)
    x1, y1 = to_m.transform(X0 + 3000, Y0 + 3000)
    code = np.full((40, 40), encode_area(np.array([0.05]))[0], np.uint8)
    code[:, :20] = encode_area(np.array([12.0]))[0]  # the west half drains 12 km²
    grid = {"code": code, "merc": [x0, y0, x1, y1]}
    west, east = _ln((0, 0), (0, 2000)), _ln((2500, 0), (2500, 2000))
    km2 = linz.line_area_km2(np.array([west, east], dtype=object), grid)
    assert 10 < km2[0] < 14 and km2[1] < 0.1

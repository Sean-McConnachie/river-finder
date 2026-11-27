"""1 m LiDAR coverage (lidar.py): survey table, coverage layer, per-reach and per-catchment coverage, gaps and the
summary, on synthetic footprints, tiles and networks (no network)."""

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
import shapely
from pyproj import Transformer
from rasterio.transform import from_origin

from river_finder import lidar as L

TO_LL = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
X0, Y0 = 1_800_000.0, 5_800_000.0   # an NZTM origin for the synthetic squares


def _coll(slug, region, start, end, title=None, cat="dem", lifecycle="completed"):
    return {"title": title or f"{region} LiDAR 1m DEM ({start[:4]})", "linz:slug": slug, "linz:region": region,
            "linz:geospatial_category": cat, "linz:lifecycle": lifecycle, "gsd": 1,
            "providers": [{"name": "LINZ", "roles": ["host"]}, {"name": "Flyer", "roles": ["producer"]},
                          {"name": "Council", "roles": ["licensor"]}],
            "extent": {"spatial": {"bbox": [[175.0, -38.0, 176.0, -37.0]]}, "temporal": {"interval": [[start, end]]}},
            "links": [{"rel": "item", "href": "./a.json"}, {"rel": "item", "href": "./b.json"}, {"rel": "self"}],
            "assets": {"capture_area": {"href": "./capture-area.geojson"}}}


def _url(region, slug):
    return f"{L.BASE}/{region}/{slug}/dem_1m/2193/collection.json"


def test_survey_record_and_mosaic_sources():
    cd = {"features": [{"properties": {"source": "s3://nz-elevation/waikato/waikato_2021/dem_1m/2193/collection.json"}},
                       {"properties": {"source": "s3://nz-elevation/otago/otago_2016/dem_1m/2193/collection.json"}}]}
    src = L.mosaic_sources(cd)
    assert src == {"waikato/waikato_2021", "otago/otago_2016"}
    r = L.survey_record(_coll("waikato_2021", "waikato", "2021-01-04T11:00:00Z", "2021-03-25T11:00:00Z"),
                        _url("waikato", "waikato_2021"), src)
    assert r["start"] == "2021-01-05" and r["end"] == "2021-03-26"   # UTC 11:00 = NZ midnight the next day
    assert r["year_start"] == 2021 and r["year"] == 2021 and r["in_mosaic"] and not r["mosaic"]
    assert r["producer"] == "Flyer" and r["licensor"] == "Council" and r["items"] == 2
    assert r["key"] == "waikato__waikato_2021"
    assert r["capture"] == _url("waikato", "waikato_2021").rsplit("/", 1)[0] + "/capture-area.geojson"
    d = L.survey_record(_coll("marl_2026", "marlborough", "2026-06-17T12:00:00Z", "2026-07-02T12:00:00Z",
                              title="Marlborough LiDAR 1m DEM (2026) - Draft", lifecycle="ongoing"),
                        _url("marlborough", "marl_2026"), src)
    assert d["draft"] and not d["in_mosaic"]
    assert L.survey_record(_coll("x", "waikato", "2021-01-01T00:00:00Z", None), _url("waikato", "x").replace(
        "dem_1m", "dsm_1m"), src) is None


def _sq(x0, y0, x1, y1):
    return shapely.box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1)


def _ll_feature(geom, props):
    g = shapely.transform(geom, lambda xy: np.column_stack(TO_LL.transform(xy[:, 0], xy[:, 1])))
    return {"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(g)}


def _layer():
    """Two mosaic surveys (west 0-2 km, east 2-3 km) and a newer survey not in the mosaic covering 2.5-4 km."""
    cd = {"type": "FeatureCollection", "features": [
        _ll_feature(_sq(0, 0, 2000, 2000), {"linz:slug": "west_2020", "linz:region": "r", "title": "West",
                                            "flown_from": "2020-01-01", "flown_to": "2021-02-01", "producer": "A",
                                            "licensor": "B", "linz:lifecycle": "completed",
                                            "source": "s3://nz-elevation/r/west_2020/dem_1m/2193/collection.json"}),
        _ll_feature(_sq(2000, 0, 3000, 2000), {"linz:slug": "east_2024", "linz:region": "r", "title": "East",
                                               "flown_from": "2024-03-01", "flown_to": "2024-04-01",
                                               "source": "s3://nz-elevation/r/east_2024/dem_1m/2193/collection.json"})]}
    rows = [L.survey_record(_coll(s, "r", a, b), _url("r", s), L.mosaic_sources(cd)) for s, a, b in (
        ("west_2020", "2020-01-01T00:00:00Z", "2021-02-01T00:00:00Z"),
        ("east_2024", "2024-03-01T00:00:00Z", "2024-04-01T00:00:00Z"),
        ("new_2026", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        ("inside_2025", "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"))]
    fps = {"r__new_2026": _sq(2500, 0, 4000, 2000), "r__inside_2025": _sq(100, 100, 900, 900)}
    return L.build_layer(rows, cd, fps)


def test_build_layer_keeps_mosaic_areas_and_adds_only_new_ground():
    layer = _layer()
    assert list(layer["slug"]) == ["west_2020", "east_2024", "new_2026"]   # inside_2025 adds nothing
    assert list(layer["code"]) == [0, 1, 2] and list(layer["in_mosaic"]) == [True, True, False]
    np.testing.assert_allclose(layer["km2"], [4.0, 2.0, 2.0], rtol=2e-3)  # new_2026 only east of 3 km
    assert layer.loc[0, "year"] == 2021 and layer.loc[0, "year_start"] == 2020 and layer.loc[1, "year"] == 2024
    assert layer.crs.to_epsg() == 2193


def test_line_coverage_shares_and_dominant_survey():
    layer = _layer()
    lines = [np.array([[X0 + 100, Y0 + 500], [X0 + 1900, Y0 + 500]]),    # west only
             np.array([[X0 + 1500, Y0 + 900], [X0 + 2500, Y0 + 900]]),    # half west, half east
             np.array([[X0 + 2600, Y0 + 100], [X0 + 4600, Y0 + 100]]),    # east 0.4 km, new 1 km, none 0.6 km
             np.array([[X0 + 9000, Y0 + 9000], [X0 + 9500, Y0 + 9000]]),  # nowhere
             np.array([[X0 + 50, Y0 + 50]])]                              # a single point
    c = L.line_coverage(lines, list(layer.geometry), layer["in_mosaic"].to_numpy(bool), step_m=10)
    np.testing.assert_allclose(c["lidar_share"], [1.0, 1.0, 0.2, 0.0, 1.0], atol=0.011)
    np.testing.assert_allclose(c["new_share"], [0.0, 0.0, 0.5, 0.0, 0.0], atol=0.011)
    assert c["lidar_code"].tolist()[0] == 0 and c["lidar_code"][2] == 2 and c["lidar_code"][3] == -1
    assert L.source_flag(c["lidar_share"], c["new_share"]).tolist() == ["lidar", "lidar", "mixed", "8m", "lidar"]


def test_catchment_counts_gap_blocks_and_components():
    lc = np.array([[-1, 5, 5, 6], [5, 5, 6, 6], [7, 7, 6, 6], [7, 7, -1, -1]])
    code = np.array([[-1, 0, 0, -1], [0, -1, 1, 1], [-1, -1, 1, 1], [-1, -1, -1, -1]])
    c = L.catchment_counts(lc, code, base=5, n_local=3, n_codes=2)
    assert c.tolist() == [[1, 3, 0], [1, 0, 4], [4, 0, 0]]   # columns: none, code 0, code 1
    land, gap = L.gap_blocks(lc >= 0, code >= 0, 2)
    assert land.tolist() == [[3, 4], [4, 2]] and gap.tolist() == [[1, 1], [4, 0]]
    lab, n = L.gap_components(land, gap, capacity=4, frac=0.5)
    assert n == 1 and lab.tolist() == [[0, 0], [1, 0]]


def test_upstream_shares_follow_the_network():
    # 0 -> 2, 1 -> 2, 2 -> 3; reach 1's local catchment has no LiDAR
    down = np.array([2, 2, 3, -1])
    cells = np.array([10, 30, 10, 50])
    lid = np.array([10, 0, 10, 50])
    lid_s, any_s = L.upstream_shares(down, cells, lid, lid)
    np.testing.assert_allclose(lid_s, [1.0, 0.0, 20 / 50, 70 / 100])
    np.testing.assert_allclose(any_s, lid_s)


def _write_tile(tmp_path, layer):
    """A 2 x 2 km tile x02y03 (8 m cells): reach 0 drains the west half, reach 1 the east half."""
    tid = "x02y03"
    d = tmp_path / tid
    d.mkdir()
    base = (3 * 100 + 2) * L.SEG_BASE
    tf = from_origin(X0, Y0 + 2000, 8.0, 8.0)
    lc = np.full((250, 250), base, np.int64)
    lc[:, 125:] = base + 1
    lc[:10, :] = -1                                                   # sea strip in the north
    with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=250, width=250, count=1,
                       dtype="int64", crs="EPSG:2193", transform=tf, nodata=-1) as ds:
        ds.write(lc, 1)
    g = gpd.GeoDataFrame({"gid": [base, base + 1]},
                         geometry=[shapely.LineString([(X0 + 500, Y0 + 1000), (X0 + 1000, Y0 + 1000)]),
                                   shapely.LineString([(X0 + 1000, Y0 + 1000), (X0 + 1900, Y0 + 1000)])],
                         crs="EPSG:2193")
    g.to_file(d / "segments_core.gpkg", driver="GPKG")
    return d, base


def test_tile_coverage_and_attach(tmp_path):
    layer = _layer()
    # cut the west survey to x < 1500 m so reach 0 is fully covered and the east half is partly
    layer.loc[0, "geometry"] = _sq(0, 0, 1500, 2000)
    layer = layer.iloc[[0]].reset_index(drop=True)
    d, base = _write_tile(tmp_path, layer)
    rec, counts, gids, (land, gap, _x0, _y1, cell_m2) = L.tile_coverage(d, layer, step_m=10)
    assert cell_m2 == 64.0
    assert rec["gid"].tolist() == [base, base + 1]
    np.testing.assert_allclose(rec["lidar_share"], [1.0, 5 / 9], atol=0.02)
    assert counts.shape == (2, 2) and gids.tolist() == [base, base + 1]
    # west catchment (x < 1000 m) fully covered; east (1000-2000 m) covered for 1000-1500 m
    np.testing.assert_allclose(counts[:, 1] / counts.sum(axis=1), [1.0, 0.5], atol=0.01)
    assert land.shape == (2, 2) and gap[:, 0].sum() == 0 and gap[:, 1].sum() > 0
    table = pd.DataFrame({"gid": [base, base + 1, 99], "down": [1, -1, -1]})
    cov = L.attach(table, rec, gids, counts, layer)
    assert cov["lidar_code"].tolist() == [0, 0, -1] and cov["lidar_missing_line"].tolist() == [False, False, True]
    np.testing.assert_allclose(cov["catch_lidar_share"].iloc[:2], [1.0, 0.75], atol=0.01)  # 1 drains 0 and 1
    assert cov["lidar_year"].tolist()[:2] == [2021, 2021] and pd.isna(cov["lidar_year"].iloc[2])
    assert cov["lidar_slug"].tolist()[:2] == ["west_2020"] * 2 and cov["lidar_in_mosaic"].tolist()[:2] == [True, True]
    assert np.isnan(cov["catch_lidar_share"].iloc[2])


def test_summarise_counts_regions_guides_and_years():
    layer = _layer()
    table = pd.DataFrame({"gid": [1, 2, 3, 4], "length_m": [1000.0, 1000.0, 2000.0, 500.0],
                          "area_km2": [5.0, 1.0, 50.0, 3.0], "region": ["A", "A", "B", "B"],
                          "river": ["Alpha", "", "Beta", "Beta"], "x": [0.0] * 4, "y": [0.0] * 4,
                          "down": [-1, -1, -1, -1]})
    cov = pd.DataFrame({"lidar_share": [1.0, 0.5, 0.0, 1.0], "new_share": [0.0, 0.0, 0.0, 0.0],
                        "lidar_code": [0, 1, -1, 1], "lidar_year": [2021, 2024, None, 2024],
                        "local_km2": [1.0, 1.0, 2.0, 1.0], "local_lidar_share": [1.0, 0.5, 0.0, 1.0],
                        "catch_lidar_share": [1.0, 0.5, 0.0, 1.0]})
    secs = [{"river": "Beta", "region": "B", "grade": "4", "held_out": False, "rows": np.array([2, 3])}]
    s = L.summarise(table, cov, layer, secs)
    n = s["national"]
    assert n["all"]["km"] == 4.5 and n["all"]["km_lidar"] == 2.0 and n["all"]["reaches_8m"] == 1
    assert n["domain"]["reaches"] == 3 and n["domain"]["km_share"] == pytest.approx(1.5 / 3.5, abs=1e-4)
    assert n["guide"]["km"] == 2.5 and n["guide"]["km_share"] == pytest.approx(0.2)
    assert n["land_share"] == pytest.approx(2.5 / 5)
    assert s["regions"]["B"]["domain"]["km_by_capture"]["2023-2026"] == 0.5
    assert s["km_by_year"] == {2021: 1.0, 2024: 0.5}
    assert s["gap_rivers"] == [{"river": "Beta", "region": "B", "km": 2.0, "max_area_km2": 50.0}]
    assert s["sections"]["n"] == 1 and s["sections"]["partial"] == 1
    assert s["sections"]["not_full"][0]["lidar_share"] == pytest.approx(0.2) and s["sections"]["not_full"][0][
        "years"] == [2024]
    json.dumps(s, default=str)   # serialisable


def test_q_tile_ids_cell_area_and_gap_grid(tmp_path):
    """A 1 m rebuild tile (32 km ``q`` id, 10 m local catchments over its box): the tile index of its gids, the
    100 m2 cell area in the local areas, and the gap grid summing the cells it owns."""
    from river_finder import nz

    assert L._tile_index("q24y32") == 13224 and L._tile_index("x05y07") == 705
    layer = _layer().iloc[[0]].reset_index(drop=True)
    tid = "q24y32"
    d = tmp_path / "tiles" / tid
    d.mkdir(parents=True)
    base = 13224 * L.SEG_BASE
    tf = from_origin(X0, Y0 + 2000, 10.0, 10.0)
    lc = np.full((200, 200), base, np.int64)
    lc[:, 100:] = base + 1
    lc[:10, :] = -1
    with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=200, width=200, count=1,
                       dtype="int64", crs="EPSG:2193", transform=tf, nodata=-1) as ds:
        ds.write(lc, 1)
    g = gpd.GeoDataFrame({"gid": [base, base + 1]},
                         geometry=[shapely.LineString([(X0 + 500, Y0 + 1000), (X0 + 1000, Y0 + 1000)]),
                                   shapely.LineString([(X0 + 1000, Y0 + 1000), (X0 + 1900, Y0 + 1000)])],
                         crs="EPSG:2193")
    g.to_file(d / "segments_core.gpkg", driver="GPKG")
    rec, counts, gids, (land, gap, x0, y1, cell_m2) = L.tile_coverage(d, layer, step_m=10)
    assert cell_m2 == 100.0 and gids.tolist() == [base, base + 1] and land.sum() == (lc >= 0).sum()
    table = pd.DataFrame({"gid": [base, base + 1], "down": [1, -1]})
    cov = L.attach(table, rec, gids, counts, layer, cell_m2=cell_m2)
    np.testing.assert_allclose(cov["local_km2"], [190 * 100 * 100 / 1e6] * 2)
    out = tmp_path / "out"
    out.mkdir()
    L.reach_coverage(layer, tmp_path / "tiles", out, step_m=10)
    z, gg = np.load(out / "catchment_cells.npz"), np.load(out / "gap_grid.npz")
    assert float(z["cell_m2"]) == 100.0 and float(gg["dem_cell_m"]) == 10.0
    assert gg["land"].sum() == (lc >= 0).sum() and gg["land"].shape == (nz.NY * 64, nz.NX * 64)


def test_dropped_gids_count_for_the_reach_that_took_them(tmp_path):
    """rf-lxa.13: the merge dropped A3 (into A2), A4 (into B1, in the next tile) and A5 (lost); the tiles' local
    catchments still name them. Their cells count for the receiving reach (one row per gid, summed over tiles); the
    land and gap blocks keep every cell."""
    layer = _layer().iloc[[0]].reset_index(drop=True)
    a1, a2, a3, a4, a5 = (13224 * L.SEG_BASE + i for i in range(1, 6))
    b1 = 13225 * L.SEG_BASE + 1
    root = tmp_path / "nz1m"
    root.mkdir()
    (root / "merge.json").write_text(json.dumps({"tiles": ["q24y32", "q25y32"], "zero_length": {
        "merged": [[a3, a2], [a4, b1], [a5, -1]]}}))
    a = np.full((100, 100), -1, np.int64)
    a[:50, :50], a[:50, 50:], a[50:75, :50], a[50:75, 50:], a[75:, :50] = a1, a2, a3, a4, a5
    for tid, x0, lc, gids in (("q24y32", X0, a, [a1, a2, a3, a4, a5]),
                              ("q25y32", X0 + 1000, np.full((100, 100), b1, np.int64), [b1])):
        d = root / "tiles" / tid
        d.mkdir(parents=True)
        with rasterio.open(d / "local_catchment_core.tif", "w", driver="GTiff", height=100, width=100, count=1,
                           dtype="int64", crs="EPSG:2193", transform=from_origin(x0, Y0 + 1000, 10.0, 10.0),
                           nodata=-1) as ds:
            ds.write(lc, 1)
        g = gpd.GeoDataFrame({"gid": gids}, geometry=[shapely.LineString([(x0 + 100 + 50 * k, Y0 + 500),
                                                                           (x0 + 140 + 50 * k, Y0 + 500)])
                                                      for k in range(len(gids))], crs="EPSG:2193")
        g.to_file(d / "segments_core.gpkg", driver="GPKG")
    out = tmp_path / "out"
    out.mkdir()
    L.reach_coverage(layer, root / "tiles", out, step_m=10)       # the map from <tiles>/../merge.json
    z = np.load(out / "catchment_cells.npz")
    cells = dict(zip(z["gid"].tolist(), z["counts"].sum(axis=1).tolist(), strict=True))
    assert cells == {a1: 2500, a2: 3750, b1: 11250} and len(z["gid"]) == len(set(z["gid"].tolist()))
    assert np.load(out / "gap_grid.npz")["land"].sum() == 8750 + 10000      # A5's cells are still land
    _, counts, gids, _ = L.tile_coverage(root / "tiles" / "q24y32", layer, step_m=10)   # no map: as the raster
    cells0 = dict(zip(gids.tolist(), counts.sum(axis=1).tolist(), strict=True))
    assert cells0[a2] == 2500 and cells0[a3] == 1250 and b1 not in cells0

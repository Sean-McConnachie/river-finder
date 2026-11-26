"""LINZ aerial photo coverage per reach (aerial.py, webdata.write_aerial): survey table, ranking, footprints and
the per-region file, on synthetic footprints (no network)."""

import json

import numpy as np
import pytest

from river_finder import aerial as A
from river_finder import webdata


def _coll(slug, gsd, start, end, cat="rural-aerial-photos", title=None):
    return {"title": title or f"Test {gsd}m Rural Aerial Photos ({start[:4]})", "linz:slug": slug, "gsd": gsd,
            "linz:geospatial_category": cat, "linz:region": "test", "linz:lifecycle": "completed",
            "extent": {"spatial": {"bbox": [[175.0, -38.0, 176.0, -37.0]]}, "temporal": {"interval": [[start, end]]}},
            "assets": {"capture_area": {"href": "./capture-area.geojson"}}}


def test_survey_record_keeps_aerial_photos_with_local_dates():
    url = f"{A.BASE}/waikato/w_2021-2023_0.3m/rgb/2193/collection.json"
    r = A.survey_record(_coll("w_2021-2023_0.3m", 0.3, "2021-12-31T11:00:00Z", "2024-03-19T11:00:00Z",
                              title="Waikato 0.3m Rural Aerial Photos (2021-2024)"), url)
    assert r["start"] == "2022-01-01" and r["end"] == "2024-03-20"  # UTC 11:00 = NZ midnight the next day
    assert r["name"] == "Waikato Rural Aerial Photos" and r["cat"] == "rural" and A.years(r) == "2022-2024"
    assert r["capture"] == url.rsplit("/", 1)[0] + "/capture-area.geojson"
    assert A.survey_record(_coll("s", 10, "2024-01-01T00:00:00Z", None, cat="satellite-imagery"), url) is None
    assert not A.candidate({"title": "Taranaki 0.375m SN8298 (1984)", "href": "./t/rgb/2193/collection.json"})
    assert not A.candidate({"title": "NZ 10m Satellite Imagery (2025)", "href": "./n/rgb/2193/collection.json"})
    assert not A.candidate({"title": "Auckland 0.075m Near-Infrared Aerial Photos", "href": "./a/rgbnir/2193/c.json"})
    assert A.candidate({"title": "Waikato 0.3m Rural Aerial Photos (2021-2024)", "href": "./w/rgb/2193/c.json"})


def _rows():
    return [A.survey_record(_coll(*a), f"{A.BASE}/x/{a[0]}/rgb/2193/collection.json") for a in (
        ("old_2010_0.125m", 0.125, "2010-01-01T00:00:00Z", "2010-02-01T00:00:00Z"),
        ("new_2024_0.3m", 0.3, "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"),
        ("town_2025_0.075m", 0.075, "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z", "urban-aerial-photos"),
        ("storm_2026_0.1m", 0.1, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "ancillary-aerial-photos"))]


def test_rank_follows_the_basemaps_layers_else_gsd_then_date():
    rows = _rows()
    layers = [{"name": "gebco", "category": "Bathymetry"}, {"name": "new-2024-0.3m", "category": "Rural Aerial Photos",
                                                            "minZoom": 13},
              {"name": "town-2025-0.075m", "category": "Urban Aerial Photos", "minZoom": 14},
              {"name": "zoomed-out-only", "category": "Rural Aerial Photos", "maxZoom": 14},
              {"name": "missing-2020-0.3m", "category": "Rural Aerial Photos"}]
    ranked, rule = A.rank(rows, layers, log=lambda *a: None)
    assert rule == "basemaps"
    assert [r["slug"] for r in ranked] == ["town_2025_0.075m", "new_2024_0.3m"]  # top layer first
    ranked, rule = A.rank(rows, None)
    assert rule == "gsd" and [r["slug"] for r in ranked] == ["town_2025_0.075m", "old_2010_0.125m", "new_2024_0.3m"]


def _square(x0, y0, x1, y1):
    import shapely

    return shapely.box(x0, y0, x1, y1)


def test_coverage_takes_the_top_survey_over_most_of_each_reach():
    lines = [np.array([[100.0, 50], [300, 50]]),     # inside the low survey only
             np.array([[300.0, 150], [700, 150]]),   # half under the low survey, half outside
             np.array([[2000.0, 0], [2100, 0]]),     # outside both
             np.array([[100.0, 900], [400, 900]]),   # under both: the top one wins
             np.array([[50.0, 600]])]               # a single point under the low survey
    ranked = [{"slug": "top"}, {"slug": "low"}]
    fps = [_square(0, 800, 1000, 1000), _square(0, 0, 500, 1000)]
    best, share = A.coverage(lines, ranked, fps, step_m=10)
    assert best.tolist() == [1, 1, -1, 0, 1]
    np.testing.assert_allclose(share, [1.0, 0.5, 0.0, 1.0, 1.0])
    row = {"title": "T", "name": "T", "gsd": 0.3, "start": "2024-01-01", "end": "2024-02-01", "cat": "rural"}
    ranked = [dict(row, slug=k) for k in ("top", "low", "unused")]
    rec = A.region_record(lines, ranked, fps + [_square(5000, 5000, 6000, 6000)], "basemaps", step_m=10)
    assert [s["id"] for s in rec["surveys"]] == ["top", "low"]  # only surveys some reach uses, best first
    assert rec["s"] == [1, 1, -1, 0, 1] and rec["part"] == [[1, 50]]
    assert rec["stats"] == {"reaches": 5, "covered": 4, "km": 1.0, "kmCovered": 0.7}


def _cache(tmp_path, monkeypatch):
    """A cache with two surveys, the Basemaps layer list and WGS84 footprints round a test river (175.5 E, 37.5 S)."""
    from pyproj import Transformer

    cache = tmp_path / "aerial"
    (cache / "capture").mkdir(parents=True)
    monkeypatch.setattr(A, "cache_dir", lambda: cache)
    rows = [A.survey_record(_coll("west_2023_0.3m", 0.3, "2023-01-05T11:00:00Z", "2023-02-01T11:00:00Z",
                                  title="West 0.3m Rural Aerial Photos (2023)"), f"{A.BASE}/x/west/collection.json"),
            A.survey_record(_coll("town_2025_0.1m", 0.1, "2024-12-20T11:00:00Z", "2025-01-10T11:00:00Z",
                                  "urban-aerial-photos", "Town 0.1m Urban Aerial Photos (2024-2025)"),
                            f"{A.BASE}/x/town/collection.json")]
    (cache / "surveys.json").write_text(json.dumps(rows))
    (cache / "basemaps_aerial.json").write_text(json.dumps([
        {"name": "west-2023-0.3m", "category": "Rural Aerial Photos", "minZoom": 13},
        {"name": "town-2025-0.1m", "category": "Urban Aerial Photos", "minZoom": 14}]))
    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    x0, y0 = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True).transform(175.5, -37.5)

    def poly(a, b, c, d):
        ring = [to_ll.transform(x0 + x, y0 + y) for x, y in ((a, b), (c, b), (c, d), (a, d), (a, b))]
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                                                           "geometry": {"type": "Polygon", "coordinates": [ring]}}]}

    (cache / "capture" / "west_2023_0.3m.geojson").write_text(json.dumps(poly(-5000, -5000, 1000, 5000)))
    (cache / "capture" / "town_2025_0.1m.geojson").write_text(json.dumps(poly(-5000, 500, 5000, 5000)))
    reach = lambda xs, y: [list(to_ll.transform(x0 + x, y0 + y)) for x in xs]  # noqa: E731
    return [reach([-2000, -1750, -1500], 0),     # west only
            reach([-500, -250, 0], 1000),        # under both: town on top
            reach([700, 1100, 1500], -1000),     # 300 of 800 m under west, the rest outside
            reach([3000, 3250, 3500], -1000)]    # under neither


def test_write_aerial_writes_the_region_file(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch)
    data = tmp_path / "viewer"
    data.mkdir()
    (data / "regions_index.json").write_text(json.dumps([
        {"id": "r1", "title": "R1", "src": "r1.json", "bounds": [[-37.6, 175.4], [-37.4, 175.6]]},
        {"id": "r2", "title": "No reaches", "src": "r2.json", "bounds": [[-38, 175], [-37, 176]]}]))
    (data / "r1.json").write_text(json.dumps({"segments": {"c": c, "len": [500] * 4}}))
    got = webdata.write_aerial(data, log=lambda *a: None)
    assert list(got) == ["r1"] and not (data / "r2_aerial.json").exists()
    rec = json.loads((data / "r1_aerial.json").read_text())
    names = [s["id"] for s in rec["surveys"]]
    assert rec["rank"] == "basemaps" and sorted(names) == ["town_2025_0.1m", "west_2023_0.3m"]
    s = [names[k] if k >= 0 else None for k in rec["s"]]
    assert s == ["west_2023_0.3m", "town_2025_0.1m", "west_2023_0.3m", None]
    part = dict(rec["part"])
    assert list(part) == [2] and part[2] == 38
    town = rec["surveys"][names.index("town_2025_0.1m")]
    assert town["gsd"] == 0.1 and town["years"] == "2024-2025" and town["name"] == "Town Urban Aerial Photos"
    assert rec["stats"]["reaches"] == 4 and rec["stats"]["covered"] == 2  # the partly covered reach is under half
    assert rec["minZoom"] == 13 and "CC BY 4.0" in rec["attribution"]


def test_write_aerial_ranks_by_gsd_without_the_basemaps_list(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch)
    (A.cache_dir() / "basemaps_aerial.json").unlink()
    monkeypatch.setattr(A, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    data = tmp_path / "viewer"
    data.mkdir()
    (data / "regions_index.json").write_text(json.dumps([{"id": "r1", "title": "R1", "src": "r1.json",
                                                          "bounds": [[-37.6, 175.4], [-37.4, 175.6]]}]))
    (data / "r1.json").write_text(json.dumps({"segments": {"c": c}}))
    webdata.write_aerial(data, log=lambda *a: None)
    rec = json.loads((data / "r1_aerial.json").read_text())
    assert rec["rank"] == "gsd" and rec["surveys"][rec["s"][1]]["id"] == "town_2025_0.1m"  # finest first


def test_unpack_coords_reads_pack_segments():
    from river_finder.viewer_nz import PACK, pack_segments

    rng = np.random.default_rng(1)
    lines = [[[175.0 + 1e-4 * k, -37.0 - 2e-4 * k] for k in range(n)] for n in (2, 5, 1, 3)]
    S = {k: [0] * len(lines) for k in PACK}
    S.update(gr=[None] * len(lines), features=["a"], z=rng.normal(size=(len(lines), 1)).tolist(), c=lines)
    blob, head = pack_segments(S)
    back = webdata.unpack_coords(blob, head)
    assert [len(b) for b in back] == [2, 5, 1, 3]
    for a, b in zip(lines, back, strict=True):
        np.testing.assert_allclose(b, np.round(np.asarray(a) * 1e5) * 1e-5, atol=1e-9)


@pytest.mark.parametrize("title,name", [("Bay of Plenty Severe Weather 0.1m Aerial Photos (2026)",
                                         "Bay of Plenty Severe Weather Aerial Photos"),
                                        ("Auckland Coast 0.05m Aerial Photos (2023) - Draft",
                                         "Auckland Coast Aerial Photos")])
def test_short_name(title, name):
    assert A.short_name(title) == name

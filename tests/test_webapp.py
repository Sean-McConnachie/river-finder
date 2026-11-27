"""The web app build: bundle order, both outputs, data linking, and that the bundled JS parses."""

import json
import re
import shutil
import subprocess

import pytest

from river_finder import webapp


def _payload(**kw):
    p = {"app": {"target": "site", "built": "2026-11-18", "commit": "", "basemapsKey": ""}, "regions": [],
         "model": None, "registry": None}
    p.update(kw)
    return p


def test_bundle_order_core_first_pages_then_start():
    js = webapp.js_files()
    assert js[: len(webapp.CORE_JS)] == list(webapp.CORE_JS)
    assert js[-1] == webapp.START_JS
    for page in webapp.PAGES:
        assert any(f.startswith(f"pages/{page}/") for f in js), f"page {page} has no script"
    css = webapp.css_files()
    assert css[:2] == list(webapp.CORE_CSS)


def test_render_inlines_everything_and_data_round_trips():
    page = webapp.render(_payload(regions=[{"id": "x", "title": "</script> X", "bounds": [[0, 0], [1, 1]]}]))
    for marker in ("/*%%CSS%%*/", "/*%%JS%%*/", "%%DATA%%", "<!--%%LIBS%%-->"):
        assert marker not in page
    assert "<html" not in page  # the artifact host adds the skeleton
    blob = re.search(r'<script type="application/json" id="rf-data">(.*?)</script>', page, re.S).group(1)
    assert json.loads(blob)["regions"][0]["title"] == "</script> X"
    for f in webapp.js_files():
        assert f"--- {f} ---" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bundled_js_parses(tmp_path):
    js = webapp._bundle(webapp.js_files(), ("/*", "*/"))
    f = tmp_path / "bundle.js"
    f.write_text(js)
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_build_writes_site_and_artifact_and_links_data(tmp_path):
    data = tmp_path / "viewer_nz"
    data.mkdir()
    (data / "regions_index.json").write_text(json.dumps([{"id": "r1", "title": "R1", "src": "r1.json",
                                                          "bounds": [[-38, 175], [-37, 176]]}]))
    (data / "r1.json").write_text("{}")
    (data / "index.html").write_text("legacy page")
    out = tmp_path / "web"
    res = webapp.build(out, data, basemaps_key="k123")
    site, art = res["site"].read_text(), res["artifact"].read_text()
    assert site.startswith("<!doctype html>") and "viewport-fit=cover" in site
    assert not art.startswith("<!doctype")
    assert (out / "r1.json").exists() and res["regions"] == 1
    assert "legacy page" not in (out / "index.html").read_text()
    assert '"basemapsKey":"k123"' in site and "k123" not in art  # keys only in the standalone site


def test_build_gives_the_site_the_logo_icon(tmp_path):
    res = webapp.build(tmp_path / "web", None, basemaps_key="")
    site, art = res["site"].read_text(), res["artifact"].read_text()
    for name in webapp.ICON_FILES:
        assert (tmp_path / "web" / name).stat().st_size > 0
    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml">' in site
    assert '<link rel="apple-touch-icon" href="apple-touch-icon.png">' in site
    assert "favicon" not in art   # the artifact host gives its own icon
    svg = (tmp_path / "web" / "favicon.svg").read_text()
    logo = re.search(r"logo: '(.*?)',", (webapp.web_root() / "core" / "rf.js").read_text()).group(1)
    for d in re.findall(r'd="([^"]+)"', logo):   # the icon draws the header logo's paths
        assert f'd="{d}"' in svg


def test_stage_pages_keeps_only_the_site(tmp_path):
    out = tmp_path / "web"
    (out / "glyphs").mkdir(parents=True)
    for name in ("index.html", "artifact.html", "index_fragment.html", "ml.html", "ml_fragment.html", "r1.json",
                 "r1_sat.jpg", "glyphs/a.txt"):
        (out / name).write_text("x")
    stage = webapp.stage_pages(out, tmp_path / "pages")
    got = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file())
    assert got == [".nojekyll", "glyphs/a.txt", "index.html", "ml.html", "r1.json"]


def test_section_record_shapes_a_research_section():
    from river_finder.labels import Section
    from river_finder.webdata import section_record, source_group

    s = Section(region="Bay of Plenty", river="Kaituna River", status="runnable", grade="4-4+",
                put_in=(176.38, -37.88), take_out=(176.39, -37.84), coords="inferred",
                extra={"id": "riverguide:x", "primary_source": "riverguide", "section": "Lowers", "grade_max": 5,
                       "description": "Pushy gorge.", "hazards": ["wood"]})
    rec = section_record(s, [3, 4], "put-in to take-out",
                         {"riverguide:x": {"sources": [{"title": "NZ RiverGuide", "url": "https://www.riverguide.co.nz/x"}]}})
    assert source_group(s) == "riverguide" and rec["src"] == "riverguide"
    assert rec["putIn"] == [-37.88, 176.38]  # [lat, lon]
    assert 4 <= rec["gnum"] < 5 and rec["gmax"] == 5
    assert rec["links"] == [{"t": "NZ RiverGuide", "u": "https://www.riverguide.co.nz/x"}] and rec["m"] == [3, 4]
    local = Section(region="kaimai", river="Tuakopae Stream", status="runnable", source="Sean: top run")
    assert source_group(local) == "local" and section_record(local, [1], "name")["desc"] == "Sean: top run"


def test_area_code_covers_every_land_cell():
    import numpy as np

    from river_finder.areacode import CODE_STEP, decode_area, encode_area

    a = np.array([np.nan, -1.0, 2.5e-5, 6.4e-5, 0.0014, 1.0, 120.0, 25_000.0])  # sea, nodata, 5 m and 8 m cells, ...
    c = encode_area(a)
    assert c[0] == 0 and c[1] == 0 and (c[2:] >= 2).all()  # one cell already has an area code
    back = decode_area(c[2:])
    assert np.all(np.abs(np.log10(back) - np.log10(a[2:])) <= CODE_STEP / 2 + 1e-9)


def test_site_payload_has_the_rain_url_and_the_artifact_none(tmp_path, monkeypatch):
    monkeypatch.delenv(webapp.RAIN_ENV, raising=False)
    assert webapp.rain_base() == webapp.RAIN_BASE and webapp.RAIN_BASE.startswith("https://raw.githubusercontent.com/")
    assert webapp.rain_base("http://localhost:8891") == "http://localhost:8891/" and webapp.rain_base("") == ""
    monkeypatch.setenv(webapp.RAIN_ENV, "http://localhost:9/rain/")
    assert webapp.rain_base() == "http://localhost:9/rain/"
    res = webapp.build(tmp_path / "web", None, basemaps_key="", rain="http://localhost:8891/")
    assert '"rainBase":"http://localhost:8891/"' in res["site"].read_text()
    assert '"rainBase":""' in res["artifact"].read_text()  # the artifact host blocks other hosts


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rain_decoder_matches_the_reference_decoder(tmp_path):
    """core/rain.js reads what rainfall/live.py writes: nulls, constant fields, log areas, u8 bits; gzip or plain."""
    import gzip

    import numpy as np

    from river_finder.rainfall import live

    n = 7

    def mm(k, x):
        return {"k": f"{k}_mm_{x}h", "kind": "u16null", "scale": 0.1, "add": 0.0, "of": k, "x": x}

    cols = [({"k": "area_km2", "kind": "u16log", "scale": live.AREA_SCALE, "add": live.AREA_ADD},
             live.quantize_area(np.array([1e-5, 0.3, 2, 45, 300, 3000, 1e5]))),
            (mm("obs", 24), live.quantize_mm(np.array([0, 0.1, 0.5, 12.34, np.nan, 250, 6553.4]))),
            (mm("fc", 24), live.quantize_mm(np.full(n, np.nan))),  # constant: no forecast
            (mm("fc", 6), live.quantize_mm(np.full(n, 2.5))),
            ({"k": "flags", "kind": "u8"}, np.array([0, 1, 2, 8, 16, 0, 1], np.uint8)),
            ({"k": "obs_prod_24h", "kind": "u8", "of": "observed", "x": 24}, np.ones(n, np.uint8))]
    raw = live.encode_region({"schema": 1, "region": "r1", "n": n, "generated": "2026-11-20T21:52:26Z",
                              "windows": [6, 24]}, cols)
    (tmp_path / "r1.bin.gz").write_bytes(gzip.compress(raw, mtime=0))
    (tmp_path / "r1.bin").write_bytes(raw)
    head, want = live.decode_region(raw)
    rain = webapp.web_root().joinpath("core/rain.js").read_text()
    (tmp_path / "run.mjs").write_text(
        "import fs from 'node:fs';\n"
        "globalThis.window = { RF: { config: { target: 'site', rainBase: 'x/' }, state: {}, emit() {} } };\n"
        "globalThis.document = { visibilityState: 'visible', addEventListener() {} };\n"
        f"(0, eval)({json.dumps(rain)});\n"
        "const out = {};\n"
        "for (const f of ['r1.bin.gz', 'r1.bin']) {\n"
        "  const { head, cols } = await window.RF.rain.decode(fs.readFileSync(new URL(f, import.meta.url)));\n"
        "  out[f] = Object.fromEntries(head.fields.map((x) => { const c = cols[x.k];\n"
        "    return [x.k, c == null || typeof c === 'number' ? { const: c }\n"
        "      : Array.from(c, (v) => (Number.isNaN(v) ? null : v))]; }));\n"
        "}\n"
        "out.m3 = [850, 3400, 120000, 4.1e6, 1.2e8].map(window.RF.rain.fmt.m3);\n"
        "console.log(JSON.stringify(out));\n")
    r = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for f in ("r1.bin.gz", "r1.bin"):
        for spec in head["fields"]:
            k, g = spec["k"], got[f][spec["k"]]
            if "const" in g:
                a = np.full(n, np.nan if g["const"] is None else g["const"], dtype=float)
            else:
                a = np.array([np.nan if v is None else v for v in g], dtype=float)
            np.testing.assert_allclose(a, want[k].astype(float), rtol=1e-6, equal_nan=True, err_msg=f"{f} {k}")
    assert got["m3"] == ["850", "3.4k", "120k", "4.1M", "120M"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("target,key", [("site", ""), ("site", "DUMMY"), ("artifact", "")])
def test_aerial_basemap_only_with_a_basemaps_key(tmp_path, target, key):
    """pages/map/20-basemaps.js: the Aerial option and its tile source exist only on a site built with a key
    (like Topo50); on the artifact the option shows but cannot be chosen (the host blocks tile servers)."""
    js = webapp.web_root().joinpath("pages/map/20-basemaps.js").read_text()
    (tmp_path / "run.mjs").write_text(
        "const out = { sources: {}, layers: {}, vis: {} };\n"
        f"const RF = {{ config: {{ target: {json.dumps(target)}, basemapsKey: {json.dumps(key)} }},\n"
        "  util: { debounce: (f) => f, toast() {} }, data: { regions: () => [] } };\n"
        "globalThis.window = { RF };\n"
        "globalThis.document = { querySelector: () => null };\n"
        "const M = RF.mapkit = { prefs: { basemap: 'linz-aerial', catch: {} }, onRegion() {}, onTheme() {},\n"
        "  token: () => '#888', isDark: () => false,\n"
        "  addLayer: (def) => { out.layers[def.id] = def; }, vis: (id, on) => { out.vis[id] = on; },\n"
        "  map: { addSource: (id, s) => { out.sources[id] = s; }, on() {}, getLayer: () => null,\n"
        "         getSource: () => null } };\n"
        f"(0, eval)({json.dumps(js)});\n"
        "M.initBasemaps();\n"
        "out.picker = M.BASEMAPS.filter((b) => !M.basemapHidden(b)).map((b) => [b.id, M.basemapAvailable(b)]);\n"
        "console.log(JSON.stringify(out));\n")
    r = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    picker = dict(got["picker"])
    if target == "site" and not key:
        assert "linz-aerial" not in picker and "topo" not in picker
        assert "bm-aerial" not in got["sources"] and got["vis"]["terrain"] and not got["vis"]["bm-aerial"]
    elif target == "site":
        assert picker["linz-aerial"] and picker["topo"]
        src = got["sources"]["bm-aerial"]
        assert src["tiles"] == ["https://basemaps.linz.govt.nz/v1/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp?api=DUMMY"]
        assert "minzoom" not in src and "minzoom" not in got["layers"]["bm-aerial"] and "LINZ" in src["attribution"]
        assert got["vis"]["bm-aerial"] and not got["vis"]["bm-sat"]  # the LINZ layer at every zoom, on its own
    else:
        assert picker["linz-aerial"] is False and "bm-aerial" not in got["sources"] and not got["vis"]["bm-aerial"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("target,key,prefs,want", [
    ("site", "DUMMY", {}, "bm-aerial"),                                   # a new visitor: the LINZ aerial photos
    ("site", "DUMMY", {"basemap": "terrain"}, "bm-aerial"),               # an old saved default, never picked
    ("site", "DUMMY", {"basemap": "topo", "basemapChosen": True}, "bm-topo"),   # a picked base map stays
    ("site", "", {}, "terrain"),                                          # no key: no aerial, terrain
    ("artifact", "", {}, "terrain"),
])
def test_default_basemap_is_linz_aerial_where_the_site_has_it(tmp_path, target, key, prefs, want):
    js = webapp.web_root().joinpath("pages/map/20-basemaps.js").read_text()
    (tmp_path / "run.mjs").write_text(
        "const out = { vis: {} };\n"
        f"const RF = {{ config: {{ target: {json.dumps(target)}, basemapsKey: {json.dumps(key)} }},\n"
        "  util: { debounce: (f) => f, toast() {} }, data: { regions: () => [] } };\n"
        "globalThis.window = { RF };\n"
        "globalThis.document = { querySelector: () => null };\n"
        f"const M = RF.mapkit = {{ prefs: Object.assign({{ basemap: 'terrain', catch: {{}} }}, {json.dumps(prefs)}),\n"
        "  onRegion() {}, onTheme() {}, token: () => '#888', isDark: () => false, savePrefs() {},\n"
        "  addLayer() {}, vis: (id, on) => { out.vis[id] = on; },\n"
        "  map: { addSource() {}, on() {}, getLayer: () => null, getSource: () => null } };\n"
        f"(0, eval)({json.dumps(js)});\n"
        "M.initBasemaps();\n"
        "out.on = Object.keys(out.vis).filter((k) => out.vis[k] && k !== 'catch');\n"
        "out.basemap = M.prefs.basemap;\n"
        "console.log(JSON.stringify(out));\n")
    r = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["on"] == [want]

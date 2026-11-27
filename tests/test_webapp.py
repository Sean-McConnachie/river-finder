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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_score_layer_colours_every_reach_from_one_expression(tmp_path):
    """pages/map/52-score.js: off by default, placed after Predicted runs; on, the model's percentiles go to MapLibre
    once as a literal array indexed by the reach (no per-reach features); "From" makes lower reaches transparent and
    a tap on one is declined; a model switch repaints with that model's scores."""
    panel = webapp.web_root().joinpath("pages/map/30-panel.js").read_text()
    js = webapp.web_root().joinpath("pages/map/52-score.js").read_text()
    (tmp_path / "run.mjs").write_text(
        "const out = { paint: [], vis: {}, sources: {}, clicks: [] };\n"
        "const handlers = {};\n"
        "const scores = { v1: [10.2, 55.5, 71, 99.6], v2: [90, 80, 20, 0.4] };\n"
        "const RF = { state: { model: 'v1', runParams: {} },\n"
        "  util: { el: () => ({}), fmt: { int: String }, css: () => '' },\n"
        "  runs: { rankOf: (p, m) => (p && p.rankBy) || (m ? 'model' : 'pareto'),\n"
        "    scores: (R, o) => (o.rankBy === 'model' ? o.model.pct : [5, 6, 7, 8]) },\n"
        "  on: (e, f) => { handlers[e] = f; }, models: { get: (id) => ({ title: `${id}: test` }) },\n"
        "  data: { scores: async (r, m) => ({ pct: scores[m], role: null }) } };\n"
        "globalThis.window = { RF };\n"
        "const feats = [0, 1, 2, 3].map((i) => ({ type: 'Feature', properties: { i, w: i / 3 }, geometry: null }));\n"
        "const layers = {};\n"
        "const M = RF.mapkit = { prefs: { layers: {}, colors: {} }, savePrefs() {}, layerInits: [],\n"
        "  card: { reach: () => ({ lines: [] }) },\n"
        "  onRegion() {}, onTheme() {}, renderTree() {}, renderLegend() {}, updateNote() {},\n"
        "  addLayer: (def) => { layers[def.id] = def; }, vis: (id, on) => { out.vis[id] = on; },\n"
        "  clickable: (c) => { out.clicks.push(c); }, reachFeature: (i) => ({ kind: 'reach', seg: i }),\n"
        "  map: { addSource: (id, s) => { out.sources[id] = s; }, getLayer: (id) => layers[id],\n"
        "    getSource: (id) => ({ setData: (d) => { out.sources[id].n = d.features.length; } }),\n"
        "    setPaintProperty: (id, k, v) => out.paint.push([id, k, v]) } };\n"
        f"(0, eval)({json.dumps(panel)});\n"
        "M.colorOf = (id, k) => `#${k}`;\n"
        "M.defineLayer({ id: 'pred', label: 'Predicted runs' });\n"
        "M.defineLayer({ id: 'pred-listed', parent: 'pred' });\n"
        "M.defineLayer({ id: 'linz', label: 'LINZ' });\n"
        f"(0, eval)({json.dumps(js)});\n"
        "out.order = M.order.slice(); out.defaultOn = M.nodes.get('score').defaultOn;\n"
        "M.layerInits.forEach((f) => f());\n"
        "M.ready = true; M.R = { id: 'r', segments: { g: [1, 1, 1, 1], reg: [1, 2, 3, 4] } };\n"
        "M.netData = { features: feats };\n"
        "const tick = () => new Promise((r) => setTimeout(r, 0));\n"
        "M.nodes.get('score').apply(); out.offVis = out.vis['score-line'];\n"
        "M.prefs.layers.score = true; M.nodes.get('score').apply(); await tick();\n"
        "const last = () => out.paint.filter((p) => p[0] === 'score-line').pop()[2];\n"
        "out.first = last(); out.lo = out.sources['src-score-lo'].n; out.onVis = out.vis['score-line'];\n"
        "const n0 = out.paint.length; M.nodes.get('score').apply(); out.repaints = out.paint.length - n0;\n"
        "M.prefs.score.min = 70; M.nodes.get('score').apply(); out.cut = last();\n"
        "const c = out.clicks[0], hit = (i) => c.feature({ properties: { i } }, null, [{ properties: { i } }]);\n"
        "out.tap = [hit(0), hit(3)];\n"
        "RF.state.model = 'v2'; handlers.model(); await tick(); out.v2 = last();\n"
        "const n1 = out.paint.length; RF.state.runParams = {}; handlers.runs(); await tick();\n"
        "out.sameScores = out.paint.length - n1;\n"
        "RF.state.runParams = { rankBy: 'wsum' }; handlers.runs(); await tick(); out.wsum = last();\n"
        "console.log(JSON.stringify(out));\n")
    r = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["order"] == ["pred", "score", "pred-listed", "linz"] and got["defaultOn"] is False
    assert got["offVis"] is False and got["onVis"] is True
    assert got["lo"] == 2  # the zoomed-out source: reaches with w >= 0.422 (30 km²)
    let, var, value, ramp = got["first"]
    assert let == "let" and var == "v" and value == ["at", ["get", "i"], ["literal", [10, 56, 71, 100]]]
    assert ramp == ["interpolate", ["linear"], ["var", "v"], 0, "#lo", 50, "#mid", 100, "#hi"]
    assert got["repaints"] == 0  # another layer toggled: same scores, same colours, nothing re-sent
    case = got["cut"][3]
    assert case[:3] == ["case", ["<", ["var", "v"], 70], "rgba(0,0,0,0)"] and case[3][3:5] == [70, "#lo"]
    assert got["tap"] == [None, {"kind": "reach", "seg": 3}]
    assert got["v2"][2] == ["at", ["get", "i"], ["literal", [90, 80, 20, 0]]]
    assert got["sameScores"] == 0  # new run-finder settings, same model percentiles: nothing repainted
    assert got["wsum"][2] == ["at", ["get", "i"], ["literal", [5, 6, 7, 8]]]  # follows the ranking in use


# Three rivers of six 250 m reaches: A big and steep, B smaller, C small and flat-sided (core/runs.js ranking test).
RUNS_RANKING_JS = """
const legacy = { run_params: { threshold: 0.5 }, default_weights: { gradient: 1, maf: 0.5 },
  descriptions: { gradient: 'Gradient' } };
const RF = { state: {}, models: { legacy, descriptions: { maf: 'Flood' } } };
globalThis.window = { RF };
(0, eval)(%%RUNS%%);
const riv = [{ maf: 50, g: 40, mel: 0.2, sig: 10, z: [0, 1], pct: 30 },
  { maf: 10, g: 60, mel: 0.15, sig: 90, z: [1, 0], pct: 20 },
  { maf: 5, g: 25, mel: 0.05, sig: 50, z: [-1, -1], pct: 99 }];
const S = { d: [], u: [], len: [], g: [], s50: [], maf: [], mel: [], hw: [], a: [], zt: [], zb: [], r: [],
  sig: [], reg: [], z: [], lab: [], ho: [], rivers: ['A', 'B', 'C'], listed: [], features: ['gradient', 'maf'], c: [] };
const pct = [];
riv.forEach((v, k) => {
  for (let j = 0; j < 6; j++) {
    const i = 6 * k + j;
    S.d.push(j < 5 ? i + 1 : -1); S.u.push(j ? i - 1 : -1); S.len.push(250); S.g.push(v.g); S.s50.push(v.g);
    S.maf.push(v.maf); S.mel.push(v.mel); S.hw.push(1); S.a.push(10); S.zt.push(100 - 10 * j); S.zb.push(90 - 10 * j);
    S.r.push(k); S.sig.push(v.sig); S.reg.push(1); S.z.push(v.z); S.lab.push(0); S.ho.push(0);
    S.c.push([[0, 0], [1, 1]]); pct.push(v.pct);
  }
});
const R = { id: 'r', segments: S, sections: [] }, model = { pct, role: null };
const P = (x) => Object.assign(RF.runs.defaults(), x);
const find = (x, o = {}) => RF.runs.find(R, Object.assign({ params: P(x), model }, o));
const names = (runs) => runs.map((r) => r.name).join('');
const out = {
  rankOf: [RF.runs.rankOf(P(), true), RF.runs.rankOf(P(), false), RF.runs.rankOf(P({ rankBy: 'sig' }), true),
    RF.runs.rankOf(null, true)],
  ids: RF.runs.rankings.map((r) => r.id), defRank: RF.runs.defaults().rankBy,
  model: names(find()),
  pareto: find({ rankBy: 'pareto' }).map((r) => [r.name, r.layer, Math.round(r.score)]),
  wsum: names(find({ rankBy: 'wsum' })),
  sig: find({ rankBy: 'sig' }).map((r) => [r.name, r.score]),
  compG: names(find({ rankBy: 'comp', featureWeights: { gradient: 1, maf: 0 } })),
  compQ: names(find({ rankBy: 'comp', featureWeights: { gradient: 0, maf: 1 } })),
  override: names(find({ rankBy: 'sig' }, { rankBy: 'model' })),
  layers: find().map((r) => [r.name, r.layer]),
  measure: Math.round(RF.runs.measure(R, [6, 7], { params: P({ rankBy: 'sig' }), model }).score),
  scoresModel: RF.runs.scores(R, { params: P(), model }) === pct,
  scoresWsum: Array.from(RF.runs.scores(R, { params: P({ rankBy: 'wsum' }), model }))
    .filter((v, i) => i % 6 === 0).map(Math.round),
  features: RF.runs.features(),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_runs_follow_the_ranking_in_the_run_params(tmp_path):
    """core/runs.js: params.rankBy ranks and scores runs (model, Pareto layer, weighted score, similarity, feature
    weights) unless rankBy is given; Pareto layers whatever the ranking; per-reach scores; the feature list."""
    js = webapp.web_root().joinpath("core/runs.js").read_text()
    (tmp_path / "run.mjs").write_text(RUNS_RANKING_JS.replace("%%RUNS%%", json.dumps(js)))
    r = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["rankOf"] == ["model", "pareto", "sig", "model"] and got["defRank"] is None
    assert got["ids"] == ["model", "pareto", "wsum", "sig", "comp"]
    assert got["model"] == "CAB"                                  # by the model's percentiles
    assert got["pareto"] == [["A", 1, 100], ["B", 2, 100], ["C", 3, 65]]   # A beats B and C on all three
    assert got["wsum"][2] == "C"                                  # A and B have full credit, C does not
    assert got["sig"] == [["B", 90], ["C", 50], ["A", 10]]
    assert got["compG"][0] == "B" and got["compQ"][0] == "A"     # your weights: gradient favours B, flood A
    assert got["override"] == "CAB"                               # opts.rankBy wins over params.rankBy
    assert got["layers"] == [["C", 3], ["A", 1], ["B", 2]]        # layers whatever the ranking
    assert got["measure"] == 90
    assert got["scoresModel"] is True and got["scoresWsum"] == [100, 100, 65]
    assert got["features"] == [{"id": "gradient", "label": "Gradient", "weight": 1},
                               {"id": "maf", "label": "Flood", "weight": 0.5}]

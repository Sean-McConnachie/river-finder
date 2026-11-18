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

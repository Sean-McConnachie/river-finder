"""Build the River Finder web app (Map, Top rivers, Models, Info) from ``src/river_finder/web/``.

The app is plain CSS and JavaScript with no bundler or npm step. ``render`` concatenates, in order:

- CSS: ``core/tokens.css``, ``core/base.css``, ``vendor/*.css``, then each page's ``pages/<id>/*.css``;
- JS: ``core/rf.js``, ``core/data.js``, ``core/rain.js``, ``core/runs.js``, ``core/detail.js``, ``core/chart.js``,
  each page's ``pages/<id>/*.js`` (files in name order), then ``core/start.js``;

into ``web/shell.html`` together with the build payload (regions index, model registry) as JSON.
Pages are independent folders, so several people (or agents) can work on different pages at once;
the shared contract is ``window.RF`` (see ``web/README.md``).

``build`` writes two pages next to the region data files (from ``river-finder nz viewer``,
``models viewer`` and ``external attach``, default ``data/viewer_nz``):

- ``index.html``: standalone document for GitHub Pages or local serving (LINZ Basemaps and live rain enabled);
- ``artifact.html``: body-only fragment for a claude.ai artifact, whose host adds the document
  skeleton and blocks every external request (no tile servers, no live rain), so the page uses its own terrain.

Live rain (``core/rain.js``, bead rf-t26.10) is read at run time from ``RAIN_BASE``: the ``rain`` branch the
hourly job publishes (``rainfall/live.py``). ``--rain-base`` or ``$RIVER_FINDER_RAIN_BASE`` point the site at
another copy, e.g. ``python -m http.server`` over the output of ``river-finder rain publish`` (CORS needed
from another port); an empty value leaves live rain out.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from importlib import resources
from pathlib import Path

from .regions import REPO_ROOT

CORE_CSS = ("core/tokens.css", "core/base.css")
CORE_JS = ("core/rf.js", "core/data.js", "core/rain.js", "core/runs.js", "core/detail.js", "core/chart.js")
START_JS = "core/start.js"
PAGES = ("map", "top", "models", "info")
# External scripts the page loads (artifact CSP: cdnjs and a few other CDNs only; exact versions).
# Their stylesheets cannot load from a CDN there, so they are vendored in web/vendor/ and inlined.
LIBS: tuple[str, ...] = ("https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.js",)
PAGE_FILES = ("index.html", "artifact.html", "index_fragment.html")
TARGETS = ("site", "artifact")
# Live rain files (index.json, <region>.bin.gz) published hourly by .github/workflows/rain.yml.
RAIN_BASE = "https://raw.githubusercontent.com/Sean-McConnachie/river-finder/rain/"
RAIN_ENV = "RIVER_FINDER_RAIN_BASE"
# Files only the artifact uses: page fragments (index, ML page) and the per-region satellite images
# (the site streams satellite tiles instead).
ARTIFACT_ONLY = ("_sat.jpg", "_fragment.html")


def web_root():
    return resources.files("river_finder").joinpath("web")


def _read(rel: str) -> str:
    return web_root().joinpath(rel).read_text(encoding="utf-8")


def _files(rel_dir: str, ext: str) -> list[str]:
    d = web_root().joinpath(rel_dir)
    if not d.is_dir():
        return []
    return sorted(f"{rel_dir}/{p.name}" for p in d.iterdir() if p.name.endswith(ext))


def css_files() -> list[str]:
    return [*CORE_CSS, *_files("vendor", ".css"), *(f for p in PAGES for f in _files(f"pages/{p}", ".css"))]


def js_files() -> list[str]:
    return [*CORE_JS, *(f for p in PAGES for f in _files(f"pages/{p}", ".js")), START_JS]


def _bundle(files: list[str], comment: tuple[str, str]) -> str:
    a, b = comment
    return "\n".join(f"{a} --- {f} --- {b}\n{_read(f)}" for f in files)


def model_summary() -> dict | None:
    """The pooled regression and run-finder defaults (``model.json``) the run finder starts from."""
    from .national import nz_dir

    for path in (nz_dir() / "model.json", REPO_ROOT / "data" / "model.json"):
        if path.exists():
            from .scoring import FEATURES

            m = json.loads(path.read_text())
            return {"coef": m["coef"], "holdout": m["holdout"], "lorocv": m["lorocv"], "run_params": m["run_params"],
                    "descriptions": m["feature_descriptions"],
                    "default_weights": {k: v[1] for k, v in FEATURES.items()}, "ha_defaults": m.get("ha_defaults", {})}
    return None


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def rain_base(value: str | None = None) -> str:
    """The live-rain base URL for the site: ``value``, else ``$RIVER_FINDER_RAIN_BASE``, else ``RAIN_BASE``
    (always ending in "/"; empty: no live rain)."""
    v = value if value is not None else os.environ.get(RAIN_ENV, RAIN_BASE)
    v = v.strip()
    return v if not v or v.endswith("/") else v + "/"


def payload(index: list[dict], target: str, basemaps_key: str = "", rain: str = "") -> dict:
    """Everything the page embeds as ``#rf-data``. ``rain``: live-rain base URL (site only)."""
    from .models import list_cards, viewer_models

    return {
        "app": {"target": target, "built": time.strftime("%Y-%m-%d"), "commit": _commit(),
                "basemapsKey": basemaps_key if target == "site" else "",
                "rainBase": rain if target == "site" else ""},
        "regions": index,
        "model": model_summary(),
        "registry": viewer_models() if list_cards() else None,
    }


def render(data: dict) -> str:
    """The page body (no document skeleton) with CSS, JS and data inlined."""
    shell = _read("shell.html")
    css = _bundle(css_files(), ("/*", "*/"))
    js = _bundle(js_files(), ("/*", "*/"))
    libs = "\n".join(f'<script src="{u}"></script>' for u in LIBS)
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    for marker, value in (("/*%%CSS%%*/", css), ("<!--%%LIBS%%-->", libs), ("/*%%JS%%*/", js), ("%%DATA%%", blob)):
        if marker not in shell:
            raise ValueError(f"web/shell.html lost its {marker} marker")
        shell = shell.replace(marker, value, 1)
    return shell


def document(body: str) -> str:
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def write_static(out_dir: Path) -> int:
    """Map label glyphs (Noto Sans PBF ranges) as base64 text: the artifact host serves no raw binary,
    and the page fetches them lazily through a custom MapLibre protocol."""
    g = out_dir / "glyphs"
    g.mkdir(exist_ok=True)
    n = 0
    for p in web_root().joinpath("vendor/glyphs").iterdir():
        if p.name.endswith(".pbf"):
            (g / (p.name[:-4] + ".txt")).write_text(base64.b64encode(p.read_bytes()).decode("ascii"))
            n += 1
    return n


def _link_data(src: Path, dst: Path) -> int:
    """Hard-link (or copy) the region data files into ``dst``; returns how many."""
    n = 0
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.name in PAGE_FILES:
            continue
        q = dst / p.name
        if q.exists() and q.stat().st_ino == p.stat().st_ino:
            n += 1
            continue
        q.unlink(missing_ok=True)
        try:
            os.link(p, q)
        except OSError:
            shutil.copy2(p, q)
        n += 1
    return n


def build(out_dir: Path, data_dir: Path | None = None, basemaps_key: str | None = None,
          rain: str | None = None) -> dict:
    """Write ``index.html`` and ``artifact.html`` into ``out_dir`` with the data files of ``data_dir``.
    ``rain``: live-rain base URL for the site (default: ``rain_base()``; "" leaves it out)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    files = 0
    if data_dir is not None and (data_dir / "regions_index.json").exists():
        index = json.loads((data_dir / "regions_index.json").read_text())
        if data_dir.resolve() != out_dir.resolve():
            files = _link_data(data_dir, out_dir)
    write_static(out_dir)
    key = basemaps_key if basemaps_key is not None else os.environ.get("LINZ_BASEMAPS_KEY", "")
    site = out_dir / "index.html"
    site.write_text(document(render(payload(index, "site", key, rain_base(rain)))), encoding="utf-8")
    art = out_dir / "artifact.html"
    art.write_text(render(payload(index, "artifact")), encoding="utf-8")
    return {"site": site, "artifact": art, "regions": len(index), "data_files": files}


def serve(out_dir: Path, port: int = 8000) -> None:
    """Serve ``out_dir`` on http://localhost:<port>/ (for local testing)."""
    import functools
    import http.server

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out_dir))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {out_dir} on http://localhost:{port}/", flush=True)
        httpd.serve_forever()


def stage_pages(out_dir: Path, stage: Path) -> Path:
    """Copy the standalone site (``index.html``, data files, glyphs; not ``artifact.html`` or the
    artifact's satellite images) into ``stage`` as a GitHub Pages tree (with ``.nojekyll``)."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for p in out_dir.rglob("*"):
        rel = p.relative_to(out_dir)
        if p.is_dir() or rel.name in ("artifact.html", "index_fragment.html") or rel.name.endswith(ARTIFACT_ONLY):
            continue
        q = stage / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
    (stage / ".nojekyll").write_text("")
    return stage


def deploy_pages(out_dir: Path, stage: Path, remote_url: str, branch: str = "gh-pages", push: bool = False) -> str:
    """Commit the staged site as a single orphan commit on ``branch``; push (force: the branch only
    ever holds the latest build) when ``push`` is set. Returns the commit id."""
    stage_pages(out_dir, stage)
    run = lambda *a: subprocess.run(["git", *a], cwd=stage, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    run("init", "-q", "-b", branch)
    run("add", "-A")
    run("-c", "user.name=river-finder", "-c", "user.email=river-finder@users.noreply.github.com",
        "commit", "-q", "-m", f"Site build {time.strftime('%Y-%m-%d %H:%M')} from {_commit() or 'working tree'}")
    sha = run("rev-parse", "--short", "HEAD")
    if push:
        run("push", "-f", remote_url, f"HEAD:{branch}")
    return sha

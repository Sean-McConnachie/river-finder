"""Everything downstream of the national 1 m merge, in one resumable command (rf-lxa.8, ``docs/nz1m-finish.md``).

``river-finder nz1m finish [--steps ...]`` rebuilds, into the 1 m root (``data/nz1m/`` by default), what today's
site reads from ``data/nz/`` and ``data/viewer_nz/``, in the order of ``docs/lidar-extraction-spec.md`` ("Rebuild
order after the pass"). Every step runs with ``nz_dir()`` pointed at the root (``national.using_root``), so the
consumers read and write there and today's 8 m site keeps working until ``river-finder nz1m switch``.

State: ``<root>/finish.json`` (per step: when it finished, seconds, peak memory, a summary). A step that is done is
skipped; ``--steps`` runs the steps named (done or not), ``--force`` re-runs done ones. A step that runs again marks
the steps that need it as not done.

Card v5 (step ``v5``) is trained here on the national 1 m table, with the ``gbm_xsec`` recipe (gbm_terrain + the 1 m
section keep list) on the ``guides-v1`` split; v1-v4 stay as archived 8 m history (``models.list_cards``). A sample
run passes ``--models-dir`` so that nothing is written to ``models/``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .regions import REPO_ROOT

# national input layers shared with the 8 m scan (linked from data/nz by the inputs step)
INPUT_FILES = ["rec2.gpkg", "flood.gpkg", "geology.gpkg", "councils.gpkg", "large_catchments.gpkg",
               "waterfalls.gpkg", "access.gpkg", "gauges.json"]
INPUT_DIRS = ["linz", "aerial", "s2tiles", "rain/gauges", "rain/scaling", "rain/cache"]
MERGE_FILES = ["network.parquet", "reaches.parquet", "x1.arrow", "steps.arrow", "lakes.gpkg", "merge.json"]
V5_ID = "v5"
BASELINE_1M = "baseline-flow-1m"
V5_RECIPE = "gbm_xsec"
SPLIT = "guides-v1"


@dataclass
class Ctx:
    root: Path
    inputs: Path                          # where the national input layers come from (data/nz)
    merge: Path                           # where the merge outputs are (default: the root)
    tiles: Path                           # where the tiles are (default: <root>/tiles)
    ref8m: Path                           # the 8 m scan, read only (comparisons): data/nz
    refs: bool = False                    # put the 5 m reference areas in the 1 m viewer
    cv_repeats: int = 5
    cv_models: list[str] | None = None
    v5_id: str = V5_ID
    v5_recipe: str = V5_RECIPE
    split: str = SPLIT
    web_steps: list[str] | None = None
    log: object = print
    notes: dict = field(default_factory=dict)

    @property
    def viewer(self) -> Path:
        return self.root / "viewer_nz"

    @property
    def web(self) -> Path:
        return self.root / "web"

    @property
    def analysis(self) -> Path:
        d = self.root / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ---------------------------------------------------------------------------------------------- memory
class PeakRSS:
    """Peak resident memory of this process while a step runs (sampled from /proc every 0.2 s), and at its start
    (what earlier steps left: the step's own need is about ``peak - start``)."""

    def __init__(self):
        self.peak, self.start, self._stop = 0.0, 0.0, threading.Event()

    @staticmethod
    def now_gb() -> float:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1e6
        except OSError:
            pass
        return 0.0

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self.now_gb())
            self._stop.wait(0.2)

    def __enter__(self):
        self.peak = self.start = self.now_gb()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, self.now_gb())


# ---------------------------------------------------------------------------------------------- inputs
LINK_MIN_BYTES = 20_000_000   # smaller files are copied: caches written in place (json, jsonl) must not be shared


def _link(src: Path, dst: Path) -> str:
    """Hard link ``src`` (following symlinks) to ``dst`` when it is a large layer (read only by every step), else
    copy. Returns how."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src).resolve()   # os.link would link a symlink itself
    if src.stat().st_size >= LINK_MIN_BYTES:
        try:
            os.link(src, dst, follow_symlinks=True)
            return "link"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copy"


def _link_tree(src: Path, dst: Path) -> dict:
    n = {"link": 0, "copy": 0, "kept": 0}
    for p in sorted(Path(src).rglob("*")):
        if p.is_dir():
            continue
        q = dst / p.relative_to(src)
        if q.exists():
            n["kept"] += 1
            continue
        n[_link(p, q)] += 1
    return n


def step_inputs(ctx: Ctx) -> dict:
    """Link the national input layers (from ``data/nz``), the merge outputs and the tiles into the root."""
    root = ctx.root
    root.mkdir(parents=True, exist_ok=True)
    out = {"files": {}, "dirs": {}, "merge": {}, "tiles": None, "missing": []}
    for f in INPUT_FILES:
        src, dst = ctx.inputs / f, root / f
        if dst.exists():
            out["files"][f] = "kept"
        elif src.exists():
            out["files"][f] = _link(src, dst)
        else:
            out["missing"].append(f)
    for d in INPUT_DIRS:
        if (ctx.inputs / d).exists():
            out["dirs"][d] = _link_tree(ctx.inputs / d, root / d)
    if ctx.merge.resolve() != root.resolve():
        for f in MERGE_FILES:
            src, dst = ctx.merge / f, root / f
            if not src.exists():
                raise SystemExit(f"no {src}: run `river-finder nz1m merge` first")
            if dst.exists():
                dst.unlink()
            out["merge"][f] = _link(src, dst)
    for f in MERGE_FILES:
        if not (root / f).exists():
            raise SystemExit(f"no {root / f}: run `river-finder nz1m merge` first (or pass --merge DIR)")
    tiles = json.loads((root / "merge.json").read_text())["tiles"]
    td = root / "tiles"
    if ctx.tiles.resolve() != td.resolve():
        td.mkdir(parents=True, exist_ok=True)
        for t in tiles:
            q = td / t
            if not q.exists():
                q.symlink_to(os.path.relpath(ctx.tiles / t, td))
        out["tiles"] = f"{len(tiles)} tiles linked from {ctx.tiles}"
    bad = [t for t in tiles if not (td / t / "tile.json").exists()
           or not json.loads((td / t / "tile.json").read_text()).get("done")]
    if bad:
        raise SystemExit(f"tiles in merge.json without a done tile.json under {td}: {', '.join(bad[:10])}")
    builds = sorted({json.loads((td / t / "tile.json").read_text()).get("build", "") for t in tiles})
    out |= {"tiles_n": len(tiles), "builds": builds,
            "reaches": json.loads((root / "merge.json").read_text())["reaches"]}
    if out["missing"]:
        ctx.log(f"inputs: missing in {ctx.inputs}: {', '.join(out['missing'])}")
    return out


# ---------------------------------------------------------------------------------------------- steps
def step_joins(ctx: Ctx) -> dict:
    from . import nz1mjoin

    return nz1mjoin.run(ctx.root, log=ctx.log)


def step_score(ctx: Ctx) -> dict:
    from .nzscore import score_national

    s = score_national(log=ctx.log)
    return {"regions": {k: v for k, v in s["regions"].items()}, "holdout": s.get("holdout")}


def step_table(ctx: Ctx) -> dict:
    from .models import national_table

    t = national_table(rebuild=True, log=ctx.log)
    return {"reaches": len(t), "columns": len(t.columns), "viewer_reaches": int((t["vi"] >= 0).sum()),
            "bytes": (ctx.root / "segments_table.pkl").stat().st_size}


def step_viewer(ctx: Ctx) -> dict:
    from .viewer_nz import build

    build(ctx.viewer, refs=None if ctx.refs else [])
    index = json.loads((ctx.viewer / "regions_index.json").read_text())
    return {"regions": len(index), "reaches": sum(e["segments"] for e in index), "bytes": _dir_bytes(ctx.viewer)}


def step_external(ctx: Ctx) -> dict:
    from . import external

    return external.attach(ctx.viewer, refs=ctx.refs, log=ctx.log)


def step_hydro(ctx: Ctx) -> dict:
    from . import hydro

    s = hydro.attach(log=ctx.log)
    return {"classes": s.get("classes")}


def step_lidar(ctx: Ctx) -> dict:
    from . import lidar

    s = lidar.run(ctx.analysis / "lidar", log=ctx.log)
    n = s["national"]
    return {"km_share": n["all"]["km_share"], "domain_km_share": n["domain"]["km_share"],
            "land_share": n["land_share"]}


def step_ml(ctx: Ctx) -> dict:
    from . import ml

    t = ml.ml_table(rebuild=True, log=ctx.log)
    return {"reaches": len(t), "columns": len(t.columns), "x1_reaches": int((t.get("x1_n", 0) > 0).sum()),
            "bytes": (ctx.root / "ml_table.pkl").stat().st_size}


def step_cv(ctx: Ctx) -> dict:
    from . import ml, mlpage

    specs = [s for s in ml.ZOO if not ctx.cv_models or s.id in ctx.cv_models]
    res = ml.run(log=ctx.log, specs=specs, repeats=ctx.cv_repeats)
    mlpage.write_explore(log=ctx.log)
    mlpage.build(ctx.viewer, log=ctx.log)
    return {"models": {m["id"]: {k: m["cv"].get(k) for k in ("auc", "auc_style", "recall_top10")}
                       for m in res["models"]}, "dir": str(ml.cv_dir())}


def step_v5(ctx: Ctx) -> dict:
    from . import ml
    from . import models as M

    if not M.card_path(BASELINE_1M).exists():   # the untrained yardstick, with this build's pool quantiles
        M.save_card(M.baseline_card(M.national_table(log=ctx.log), BASELINE_1M) | {"data": "1m"})
    p = M.card_path(ctx.v5_id)
    if p.exists():
        c = json.loads(p.read_text())
        if M.card_data(c) != "1m":
            raise SystemExit(f"{p} exists and is not a 1 m card: pick another --v5-id")
        ctx.log(f"v5: {p} exists (cards are frozen); delete it and its booster to train again")
        return {"card": str(p), "trained": False, "positives": c["labels"]["positives"]}
    t = ml.ml_table(log=ctx.log)
    mapped = M.map_sections(t, M.all_sections())
    title = "v5: LightGBM on the 1 m LiDAR rebuild"
    desc = ("gbm_terrain + the 1 m section keep list (gbm_xsec), trained on the national 1 m LiDAR rebuild "
            "(2 m routing, network areas, LINZ names). The site default from the 1 m switch; v1-v4 are 8 m history.")
    card, text = ml.train_card(t, mapped, M.load_split(ctx.split), ctx.v5_id, title, desc, spec_id=ctx.v5_recipe)
    card["data"] = "1m"
    ml.save_gbm(card, text)
    audit = ml.write_audit(ctx.v5_id, log=ctx.log)
    return {"card": str(p), "trained": True, "recipe": ctx.v5_recipe, "positives": card["labels"]["positives"],
            "training_reaches": card["labels"]["training_reaches"], "train_seconds": card["train_seconds"],
            "audit": str(audit)}


def step_evaluate(ctx: Ctx) -> dict:
    from . import models as M

    t = M.national_table(log=ctx.log)
    out = M.evaluate_all(t, M.map_sections(t, M.all_sections()), log=ctx.log)
    return {"evaluations": len(out["evaluations"]), "file": str(M.evaluations_path()),
            "headline": [{k: e.get(k) for k in ("model", "split", "coords", "auc", "auc_style_matched")}
                         for e in out["evaluations"] if e.get("coords") == "any"]}


def step_scores(ctx: Ctx) -> dict:
    from . import models as M
    from .viewer_nz import build

    cards = M.write_viewer_scores(ctx.viewer, log=ctx.log)
    build(ctx.viewer, page_only=True)
    return {"cards": [c["id"] for c in cards]}


def step_web(ctx: Ctx) -> dict:
    from . import webapp
    from .linz import write_linz
    from .webdata import write_access, write_aerial, write_gauges, write_hydro, write_satellite, write_sections

    steps = {"sections": write_sections, "access": write_access, "gauges": write_gauges, "linz": write_linz,
             "hydro": write_hydro, "aerial": write_aerial, "satellite": write_satellite}
    out = {}
    for k, fn in steps.items():
        if ctx.web_steps and k not in ctx.web_steps:
            continue
        t0 = time.time()
        try:
            fn(ctx.viewer)
            out[k] = {"ok": True, "seconds": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001 (one failed extra, e.g. no network, must not stop the others)
            out[k] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            ctx.log(f"web data {k}: FAILED {out[k]['error']}")
    b = webapp.build(ctx.web, ctx.viewer, None, "")   # preview: no Basemaps key, no live rain (until the switch)
    out["build"] = {"site": str(b["site"]), "regions": b["regions"], "data_files": b["data_files"],
                    "bytes": _dir_bytes(ctx.web)}
    return out


def step_rain(ctx: Ctx) -> dict:
    from .rainfall import pipeline
    from .rainfall.config import load_config
    from .rainfall.live import build_inputs
    from .rainfall.zones import run_zones

    cfg = load_config(nz_data=ctx.root)
    pipeline.run_weights(cfg, list(cfg.grids), log=ctx.log)
    zm = run_zones(cfg, log=ctx.log)
    net = pipeline.load_network(cfg, rebuild=True, log=ctx.log)
    out_dir = ctx.root / "rain-inputs"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    man = build_inputs(cfg, out_dir, viewer_dir=ctx.viewer, log=ctx.log)
    return {"network": {"reaches": net.n, "flags": net.flag_counts()}, "zones": len(zm.names),
            "inputs": str(out_dir), "inputs_bytes": _dir_bytes(out_dir), "files": len(man.get("files", []))}


def step_export(ctx: Ctx) -> dict:
    from .nz import export_gis

    out = export_gis(log=ctx.log)
    return {"dir": str(out), "bytes": _dir_bytes(out)}


def step_compare(ctx: Ctx) -> dict:
    from .nz1mcompare import compare_references

    res = compare_references(ctx.root, log=ctx.log)
    p = ctx.analysis / "reference_compare.json"
    p.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=_json_default))
    return {"file": str(p), "regions": {k: v.get("status") for k, v in res["regions"].items()}}


def step_sections(ctx: Ctx) -> dict:
    from .nz1mcompare import section_audit

    res, rows = section_audit(ctx.root, ctx.ref8m, log=ctx.log)
    p = ctx.analysis / "sections_audit.json"
    p.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=_json_default))
    rows.to_csv(ctx.analysis / "sections_audit.csv", index=False)
    return {"file": str(p), **{k: v for k, v in res.items() if k != "rivers"}}


@dataclass(frozen=True)
class Step:
    name: str
    fn: object
    needs: tuple[str, ...]
    doc: str


STEPS = [
    Step("inputs", step_inputs, (), "link the national input layers, the merge outputs and the tiles into the root"),
    Step("joins", step_joins, ("inputs",), "geology, LINZ names and distance, LiDAR survey, sinuosity -> "
         "joins.parquet"),
    Step("score", step_score, ("joins",), "nz score: regions/*/segments_scored.gpkg, model.json, candidates.csv"),
    Step("table", step_table, ("score",), "models table: segments_table.pkl (with the 1 m fields)"),
    Step("viewer", step_viewer, ("table",), "nz viewer: <root>/viewer_nz region files and page"),
    Step("external", step_external, ("viewer",), "external attach: external.pkl, <region>_x.txt, _falls.json"),
    Step("hydro", step_hydro, ("table",), "hydro attach: hydro.pkl"),
    Step("lidar", step_lidar, ("table",), "lidar coverage on the 1 m tiles: <root>/analysis/lidar"),
    Step("ml", step_ml, ("external",), "ml table: ml_table.pkl (with x1_*)"),
    Step("cv", step_cv, ("ml",), "ml cv + page: models/cv-1m/, <root>/viewer_nz/ml.html"),
    Step("v5", step_v5, ("ml",), "card v5 (gbm_xsec on guides-v1), its audit, the 1 m flood-size baseline"),
    Step("evaluate", step_evaluate, ("v5",), "models evaluate (1 m cards): models/evaluations-1m.json"),
    Step("scores", step_scores, ("v5", "viewer"), "models viewer: <region>_m_<card>.txt for the 1 m cards"),
    Step("web", step_web, ("scores", "external", "hydro"), "web data + web build -> <root>/web (preview)"),
    Step("rain", step_rain, ("viewer",), "rain weights, zones, network, inputs -> <root>/rain, <root>/rain-inputs"),
    Step("export", step_export, ("score",), "nz export: <root>/gis"),
    Step("compare", step_compare, ("table",), "1 m national vs the 5 m reference areas -> analysis/"),
    Step("sections", step_sections, ("table",), "section placement audit, 8 m vs 1 m -> analysis/"),
]
BY_NAME = {s.name: s for s in STEPS}


def _dependents(name: str) -> set[str]:
    out, todo = set(), [name]
    while todo:
        n = todo.pop()
        for s in STEPS:
            if n in s.needs and s.name not in out:
                out.add(s.name)
                todo.append(s.name)
    return out


def _dir_bytes(d: Path) -> int:
    return sum(p.stat().st_size for p in Path(d).rglob("*") if p.is_file()) if Path(d).exists() else 0


def _json_default(o):
    if isinstance(o, np.integer | np.floating | np.bool_):
        return o.item()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def state_path(root: Path) -> Path:
    return Path(root) / "finish.json"


def load_state(root: Path) -> dict:
    p = state_path(root)
    return json.loads(p.read_text()) if p.exists() else {"steps": {}}


def save_state(root: Path, st: dict) -> None:
    p = state_path(root)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False, default=_json_default))
    os.replace(tmp, p)


def finish(root: Path | None = None, steps: list[str] | None = None, force: bool = False, merge: Path | None = None,
           tiles: Path | None = None, inputs: Path | None = None, ref8m: Path | None = None,
           models_dir: Path | None = None, log=print, **opts) -> dict:
    """Run the downstream steps into ``root`` (default ``data/nz1m``). See the module doc."""
    from . import models as M
    from .national import is_1m, using_root
    from .nz1m import root_dir

    root = Path(root or root_dir())
    unknown = [s for s in steps or [] if s not in BY_NAME]
    if unknown:
        raise SystemExit(f"unknown steps {unknown}: {', '.join(BY_NAME)}")
    ctx = Ctx(root=root, inputs=Path(inputs or REPO_ROOT / "data" / "nz"), merge=Path(merge or root),
              tiles=Path(tiles or root / "tiles"), ref8m=Path(ref8m or REPO_ROOT / "data" / "nz"), log=log, **opts)
    if models_dir is not None:
        M.set_models_dir(Path(models_dir))
    st = load_state(root) if root.exists() else {"steps": {}}
    todo = [s for s in STEPS if (s.name in steps if steps else (force or s.name not in st["steps"]))]
    log(f"finish: root {root}, steps {', '.join(s.name for s in todo) or '(none: all done)'}; models in "
        f"{M.MODELS_DIR}")
    t_all = time.time()
    with using_root(root):
        for s in todo:
            missing = [n for n in s.needs if n not in st["steps"]]
            if missing:
                raise SystemExit(f"step {s.name} needs {', '.join(missing)} first")
            if s.name != "inputs" and not is_1m(root):
                raise SystemExit(f"{root} holds no network.parquet: run the inputs step (or nz1m merge) first")
            log(f"== {s.name}: {s.doc}")
            t0 = time.time()
            with PeakRSS() as mem:
                summary = s.fn(ctx)
            rec = {"done": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "seconds": round(time.time() - t0, 1),
                   "peak_rss_gb": round(mem.peak, 2), "start_rss_gb": round(mem.start, 2), "summary": summary}
            later = {x.name for x in todo[todo.index(s) + 1:]}
            for d in _dependents(s.name):   # what was built on the old result is not done any more
                if st["steps"].pop(d, None) is not None and d not in later:
                    st.setdefault("stale", {})[d] = f"{s.name} ran again at {rec['done']}"
            st["steps"][s.name] = rec
            st.get("stale", {}).pop(s.name, None)
            st["root"], st["models_dir"] = str(root), str(M.MODELS_DIR)
            save_state(root, st)
            log(f"== {s.name}: done in {rec['seconds']} s, peak {rec['peak_rss_gb']} GB")
    st["seconds_last_run"] = round(time.time() - t_all, 1)
    save_state(root, st)
    return st


# ---------------------------------------------------------------------------------------------- switch
REQUIRED_FOR_SWITCH = ("score", "table", "viewer", "external", "hydro", "ml", "v5", "scores", "web", "rain")


def switch(root: Path | None = None, back: bool = False, force: bool = False, log=print) -> Path:
    """Point ``config/national.toml`` at the 1 m root (``back``: at ``data/nz`` again). Refuses while a step the site
    needs is not done (``force`` overrides). The site itself is rebuilt and published by hand afterwards."""
    from .national import DEFAULT_ROOT, ROOT_CONFIG
    from .national import REPO_ROOT as RR
    from .nz1m import root_dir

    root = Path(root or root_dir())
    if back:
        target = DEFAULT_ROOT
    else:
        st = load_state(root)
        missing = [s for s in REQUIRED_FOR_SWITCH if s not in st["steps"]]
        if missing and not force:
            raise SystemExit(f"not switching: {', '.join(missing)} not done in {state_path(root)} "
                             f"(river-finder nz1m finish), or pass --force")
        target = os.path.relpath(root.resolve(), RR) if root.resolve().is_relative_to(RR) else str(root.resolve())
    text = ROOT_CONFIG.read_text() if ROOT_CONFIG.exists() else "[data]\nroot = \"data/nz\"\n"
    lines = [f'root = "{target}"' if ln.strip().startswith("root =") else ln for ln in text.splitlines()]
    if not any(ln.startswith("root =") for ln in lines):
        lines += ["[data]", f'root = "{target}"']
    ROOT_CONFIG.write_text("\n".join(lines) + "\n")
    log(f"switch: {ROOT_CONFIG} root = {target}. Next (docs/nz1m-finish.md): commit it with models/{V5_ID}.json, "
        f"{V5_ID}.lgb.txt, {BASELINE_1M}.json, evaluations-1m.json, cv-1m/, then `river-finder web build`, "
        f"`web pages --push`, and refresh the rain-inputs branch from {Path(target) / 'rain-inputs'} "
        f"(docs/live-rain.md)")
    return ROOT_CONFIG


def status(root: Path | None = None, log=print) -> dict:
    from .nz1m import root_dir

    root = Path(root or root_dir())
    st = load_state(root)
    for s in STEPS:
        r = st["steps"].get(s.name)
        stale = st.get("stale", {}).get(s.name)
        log(f"{s.name:<9} " + (f"done {r['done']}, {r['seconds']} s, {r['peak_rss_gb']} GB" if r else
                               f"not done{' (' + stale + ')' if stale else ''}") + f"  - {s.doc}")
    return st


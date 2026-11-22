"""Command-line entry point: ``river-finder <command> <region> ...``."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .regions import REPO_ROOT, list_regions, load_region


def _fetch(region, args):
    from . import context, rec2
    from .stac import hybrid_mosaic, mosaic

    cache, d = REPO_ROOT / "data" / "_stac", region.data_dir()
    t = time.time()
    p = mosaic(region.dem_coarse, region.bbox_wgs84, region.bbox_nztm(8.0), 8.0, d / "dem_8m.tif", cache)
    print(f"[{region.name}] 8 m DEM -> {p} ({time.time() - t:.0f}s)")
    if region.lidar and region.routing_res < 8.0:
        t, res = time.time(), region.routing_res
        p = hybrid_mosaic(region.lidar, region.dem_coarse, region.bbox_wgs84, region.bbox_nztm(res), res,
                          d / f"dem_{res:g}m.tif", cache)
        print(f"[{region.name}] {res:g} m LiDAR hybrid -> {p} ({time.time() - t:.0f}s)")
    rec2.fetch_region(region.bbox_wgs84, d)
    context.fetch_region(region, d)
    print(f"[{region.name}] REC2 reaches, GNS geology, NIWA flood statistics -> {d}")


def _route(region, args):
    from .products import run_routing

    print(f"[{region.name}] routing:", json.dumps(run_routing(region)))


def _validate(region, args):
    from .validate import validate

    s = validate(region)
    agreement = json.dumps(s["tolerant"]["gt_5km2"])
    print(f"[{region.name}] REC2 agreement (reaches > 5 km2, tolerant snap): {agreement}")
    for n in s["named"]:
        model, ref = n.get("model_km2"), n.get("rec2_km2")
        print(f"   {n['river']:26s} {n.get('status', ''):9s} model {model} km2 vs REC2 {ref} km2")


def _reaches(region, args):
    from .reaches import run

    t = time.time()
    print(f"[{region.name}] reach segments -> {run(region)} ({time.time() - t:.0f}s)")


def _score(regions, args):
    from .scoring import score_and_write

    summary = score_and_write(regions, REPO_ROOT / "data")
    print("score:", json.dumps(summary, indent=1))


def _export(region, args):
    from .export import export_region

    print(f"[{region.name}] GIS export -> {export_region(region)}")


def _nz(args):
    from . import national, nz

    t0 = time.time()

    def log(m):
        print(f"[{(time.time() - t0) / 60:6.1f} min] {m}", flush=True)

    tiles = [nz.Tile.parse(t) for t in args.tiles] or None
    if args.step == "fetch":
        national.fetch_all(log)
    elif args.step == "tiles":
        nz.run_tiles(tiles, log=log)
    elif args.step == "export":
        nz.export_gis(log)
    elif args.step == "relink":
        _relink(args, log)
    elif args.step == "score":
        from .nzscore import score_national

        score_national(tiles, log=log)
    else:
        from .viewer_nz import build

        log(f"viewer -> {build(args.out, page_only=args.page_only)}")


def _relink(args, log):
    import shutil

    import pandas as pd

    from .national import nz_dir
    from .relink import check_network, relink_table

    data = args.data or nz_dir()
    src = args.table or data / "segments_table.pkl"
    dst = args.table_out or src
    t = pd.read_pickle(src)
    fixed, lg = relink_table(t, data / "tiles", data / "rec2.gpkg", log=log)
    for name, f in (("before", t), ("after", fixed)):
        r = check_network(f["down"].to_numpy(), f["area_km2"].to_numpy())
        log(f"{name}: " + json.dumps({k: v for k, v in r.items() if not k.startswith("_")}))
    if dst == src:
        shutil.copy2(src, src.with_name(src.stem + ".pre_relink.pkl"))
    fixed.to_pickle(dst)
    out_log = dst.with_name(dst.stem + "_relink_log.csv")
    lg.to_csv(out_log, index=False)
    log(f"relinked table -> {dst}; log -> {out_log}")


def _models(args):
    from . import models as M

    t0 = time.time()

    def log(m):
        print(f"[{(time.time() - t0) / 60:6.1f} min] {m}", flush=True)

    if args.step == "import":
        from .research import load_research, summary

        print(json.dumps(summary(load_research(args.labels)), indent=1, ensure_ascii=False))
        return
    if args.step == "viewer":   # reference regions need no national table (write_viewer_scores loads it if needed)
        from .viewer_nz import build

        M.write_viewer_scores(args.out, log=log)
        log(f"viewer page -> {build(args.out, page_only=True)}")
        return
    table = M.national_table(rebuild=args.step == "table", log=log)
    if args.step == "table":
        log(f"table: {len(table)} reaches")
    elif args.step == "checkpoint":
        card = M.checkpoint_v1(table)
        log(f"checkpoint -> {M.save_card(card)}: {json.dumps(card['checkpoint_check'])}")
    elif args.step == "baseline":
        log(f"baseline -> {M.save_card(M.baseline_card(table))}")
    elif args.step == "split":
        mapped = M.map_sections(table, M.all_sections(args.labels))
        sp = M.make_split(mapped, args.name or "guides-v1", seed=args.seed, test_frac=args.test_frac)
        log(f"split -> {M.save_split(sp)}: {len(sp['train'])} train rivers, {len(sp['test'])} test rivers")
    elif args.step == "train":
        if not args.name:
            raise SystemExit("models train needs --name (the new model id, e.g. v2)")
        if args.kind == "gbm":
            from . import ml

            t = ml.ml_table(log=log)
            mapped = M.map_sections(t, M.all_sections(args.labels))
            card, text = ml.train_card(t, mapped, M.load_split(args.split), args.name, args.title or args.name,
                                       args.description or "", spec_id=args.recipe)
            log(f"train -> {ml.save_gbm(card, text)}: {card['labels']['positives']} positive reaches, "
                f"{card['train_seconds']} s")
        else:
            mapped = M.map_sections(table, M.all_sections(args.labels))
            card = M.train(table, mapped, M.load_split(args.split), args.name, args.title or args.name,
                           args.description or "", variant=args.variant)
            log(f"train -> {M.save_card(card)}: {card['labels']['positives']} positive reaches")
    elif args.step == "evaluate":
        M.evaluate_all(table, M.map_sections(table, M.all_sections(args.labels)), log=log)


def _external(args):
    import json

    from . import external as X

    if args.step in ("fetch", "all"):
        X.fetch(log=print)
    if args.step in ("attach", "all"):
        out = args.out if args.out.exists() else None
        print(json.dumps(X.attach(out, log=print), indent=1))


def _ml(args):
    from . import ml, mlpage

    t0 = time.time()

    def log(m):
        print(f"[{(time.time() - t0) / 60:6.1f} min] {m}", flush=True)

    if args.step in ("table", "all"):
        ml.ml_table(rebuild=True, log=log)
    if args.step in ("explore", "all"):
        mlpage.write_explore(log=log)
    if args.step in ("cv", "all"):
        specs = [s for s in ml.ZOO if not args.models or s.id in args.models]
        ml.run(log=log, specs=specs, repeats=args.repeats)
    if args.step == "audit":
        ml.write_audit(args.model, n=args.top, log=log)
    if args.step in ("page", "all"):
        mlpage.build(args.out, log=log)


def _rain(args):
    from .rainfall import pipeline
    from .rainfall.config import load_config

    t0 = time.time()

    def log(m):
        print(f"[{(time.time() - t0) / 60:6.1f} min] {m}", flush=True)

    cfg = load_config(args.config, args.data)
    if args.step == "weights":
        fams = list(cfg.grids) if args.grid == "all" else [args.grid]
        pipeline.run_weights(cfg, fams, log=log)
    elif args.step == "ecmwf":
        from .rainfall.ecmwf import parse_hour
        from .rainfall.fetch import FetchError, Http, NotFound

        both = not (args.obs or args.fc)
        now = parse_hour(args.now) if args.now else None
        http = Http(retries=args.retries, log=log) if args.retries is not None else None
        try:
            pipeline.run_ecmwf(cfg, run=args.run, obs=args.obs or both, fc=args.fc or both, reader=args.reader,
                               now=now, cache=not args.no_cache, http=http, log=log)
        except (pipeline.SourceUnavailable, FetchError, NotFound) as e:
            # late or unreachable data, not a bug: a distinct exit code lets the hourly job keep the last files
            log(f"ECMWF unavailable: {e.code if isinstance(e, SystemExit) else e}")
            raise SystemExit(pipeline.EXIT_UNAVAILABLE) from None
    elif args.step == "gauges":
        from dataclasses import replace

        from .rainfall import gauges
        from .rainfall.ecmwf import parse_hour

        now = parse_hour(args.now) if args.now else None
        if args.retries is not None:
            cfg = replace(cfg, gauges=replace(cfg.gauges, retries=args.retries))
        try:
            if args.discover:
                gauges.discover(cfg, councils=args.council, now=now, log=log)
                return
            rep = gauges.run_gauges(cfg, councils=args.council, days=args.days, sites_only=args.sites_only, now=now,
                                    refetch_hours=args.refetch_hours, log=log)
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from None
        if not rep["answered"]:
            # no council server answered: unreachable or down, not a bug (the cache and outputs are kept)
            raise SystemExit(pipeline.EXIT_UNAVAILABLE)
    elif args.step == "scale":
        from .rainfall import scaling
        from .rainfall.ecmwf import parse_hour
        from .rainfall.fetch import FetchError, Http, NotFound

        now = parse_hour(args.now) if args.now else None
        http = Http(retries=args.retries if args.retries is not None else 5, log=log)
        try:
            scaling.run_scale(cfg, days=args.days, now=now, force=args.force, fetch=not args.no_fetch, http=http,
                              log=log)
        except (FetchError, NotFound) as e:
            log(f"ECMWF unavailable for the cell store: {e}")
            raise SystemExit(pipeline.EXIT_UNAVAILABLE) from None
    elif args.step == "validate":
        from .rainfall.fetch import FetchError, Http, NotFound
        from .rainfall.validate import run_validate

        if not args.event:
            raise SystemExit("rain validate needs --event START END (UTC hours YYYYMMDDHH)")
        http = Http(retries=args.retries if args.retries is not None else 5, log=log)
        try:
            run_validate(cfg, args.event[0], args.event[1], regions=args.region, catchments=args.catchment,
                         fetch=not args.no_fetch, margin=args.margin, out_dir=args.out, exclude=args.exclude_gauge,
                         http=http, log=log)
        except (FetchError, NotFound) as e:
            log(f"ECMWF unavailable for the event: {e}")
            raise SystemExit(pipeline.EXIT_UNAVAILABLE) from None
    elif args.step == "zones":
        from .rainfall.zones import run_zones

        run_zones(cfg, log=log)
    elif args.step == "totals":
        pipeline.run_totals(cfg, windows=args.windows, scaled=not args.raw, rebuild_network=args.rebuild_network,
                            log=log)
    elif args.step == "inputs":
        from .rainfall.live import build_inputs

        if args.out is None:
            raise SystemExit("rain inputs needs --out DIR")
        build_inputs(cfg, args.out, viewer_dir=args.viewer, log=log)
    elif args.step == "publish":
        from .rainfall.live import publish

        if args.out is None:
            raise SystemExit("rain publish needs --out DIR")
        publish(cfg, args.out, log=log)
    else:
        pipeline.status(cfg, log=print)


STEPS = {"fetch": _fetch, "route": _route, "validate": _validate, "reaches": _reaches, "export": _export}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="river-finder", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in [*STEPS, "score", "run"]:
        p = sub.add_parser(name, help="all steps + score + viewer" if name == "run" else f"{name} step")
        p.add_argument("regions", nargs="*", help=f"region names (default: all of {list_regions()})")
    pn = sub.add_parser("nz", help="national run: fetch | tiles | score | export | viewer | relink "
                                   "(see docs/PLAN.md)")
    pn.add_argument("step", choices=["fetch", "tiles", "score", "viewer", "export", "relink"],
                    help="relink: reconnect, along REC2, reaches of the national table that stop at a tile edge "
                         "(rf-t26.8), without re-running tiles or score")
    pn.add_argument("tiles", nargs="*", help="tile ids such as x11y16 (default: every land tile)")
    pn.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz")
    pn.add_argument("--page-only", action="store_true", help="viewer: re-render only the page, keep the region files")
    pn.add_argument("--data", type=Path, default=None,
                    help="relink: national data directory with tiles/, rec2.gpkg and segments_table.pkl "
                         "(default data/nz)")
    pn.add_argument("--table", type=Path, default=None,
                    help="relink: table to read (default <data>/segments_table.pkl)")
    pn.add_argument("--table-out", type=Path, default=None,
                    help="relink: where to write the relinked table (default: replace --table, keeping the old one "
                         "as <name>.pre_relink.pkl); the link log goes next to it as <name>_relink_log.csv")
    pm = sub.add_parser("models", help="model registry: table | checkpoint | baseline | import | split | "
                                       "train | evaluate | viewer (see src/river_finder/models.py)")
    pm.add_argument("step", choices=["table", "checkpoint", "baseline", "import", "split", "train", "evaluate",
                                         "viewer"])
    pm.add_argument("--labels", type=Path, default=None, help="research JSON (default labels/guide_research.json)")
    pm.add_argument("--name", help="split name (split) or new model id (train)")
    pm.add_argument("--split", default="guides-v1", help="train: split to use")
    pm.add_argument("--variant", choices=["all", "sourced"], default="all",
                    help="train: 'sourced' drops sections whose coordinates were inferred")
    pm.add_argument("--kind", choices=["logistic", "gbm"], default="logistic",
                    help="train: logistic regression on the 13 features, or LightGBM with an ML recipe")
    pm.add_argument("--recipe", default="gbm_terrain", help="train --kind gbm: model-zoo id whose inputs to use "
                                                             "(see ml.ZOO)")
    pm.add_argument("--title")
    pm.add_argument("--description")
    pm.add_argument("--seed", type=int, default=20261113)
    pm.add_argument("--test-frac", type=float, default=0.25)
    pm.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz")
    px = sub.add_parser("external", help="waterfalls, rapids, roads and tracks from OpenStreetMap via Overture "
                                         "Maps: fetch | attach | all (see src/river_finder/external.py)")
    px.add_argument("step", choices=["fetch", "attach", "all"])
    px.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz",
                    help="attach: viewer directory to write <region>_x.txt / _falls.json into")
    pw = sub.add_parser("web", help="web app (Map, Top rivers, Models, Info): build | serve (see web/README.md)")
    pw.add_argument("step", choices=["data", "build", "serve", "pages"],
                    help="data: per-region extras (sections, roads, tracks, gauges, LINZ rivers, satellite) into "
                         "--data; build: the pages; serve: localhost; pages: stage the site for GitHub Pages "
                         "(--push to publish)")
    pw.add_argument("--push", action="store_true", help="pages: force-push the staged site to the gh-pages branch")
    pw.add_argument("--only", nargs="+", choices=["sections", "access", "gauges", "linz", "satellite"],
                    help="data: run only these steps (e.g. --only linz rebuilds <region>_linz.json from the cached "
                         "LINZ lines, no network)")
    pw.add_argument("--data", type=Path, default=REPO_ROOT / "data" / "viewer_nz",
                    help="region data files (from nz viewer / models viewer / external attach)")
    pw.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "web")
    pw.add_argument("--basemaps-key", default=None, help="LINZ Basemaps API key for the standalone site "
                                                          "(default: $LINZ_BASEMAPS_KEY)")
    pw.add_argument("--rain-base", default=None,
                    help="build: live-rain base URL for the standalone site (default: $RIVER_FINDER_RAIN_BASE, else "
                         "the rain branch on raw.githubusercontent.com; '' leaves live rain out)")
    pw.add_argument("--port", type=int, default=8000)
    pl = sub.add_parser("ml", help="ML evaluation and page: table | explore | cv | page | audit | all "
                                   "(see src/river_finder/ml.py, mlpage.py)")
    pl.add_argument("step", choices=["table", "explore", "cv", "page", "audit", "all"])
    pl.add_argument("--model", default="v3", help="audit: registry model id")
    pl.add_argument("--top", type=int, default=60, help="audit: how many rivers")
    pl.add_argument("--models", nargs="*", help="cv: only these model ids (default: the whole zoo)")
    pl.add_argument("--repeats", type=int, default=5, help="cv: repeats of the 5-fold split")
    pl.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz")
    pr = sub.add_parser("rain", help="rainfall per catchment: weights | ecmwf | gauges | zones | scale | totals | "
                                     "inputs | publish | status (see src/river_finder/rainfall/, docs/live-rain.md, "
                                     "docs/rain-gauges.md, docs/rainfall-scaling.md)")
    pr.add_argument("step", choices=["weights", "ecmwf", "gauges", "zones", "scale", "totals", "validate", "inputs",
                                     "publish", "status"],
                    help="weights: grid-cell -> local-catchment matrices into <data>/rain/; ecmwf: ECMWF IFS observed "
                         "proxy and next-24-h forecast into <data>/rain/obs_ecmwf.npz and fc_ecmwf.npz (exit 75 when "
                         "no run is published or the bucket does not answer); gauges: hourly rain-gauge totals from "
                         "the councils' Hilltop servers (WCRC, TDC) and ECan's Open Data Portal into "
                         "<data>/rain/gauges/ (gauges_hourly.npz + .json; exit 75 when no council answers); zones: "
                         "the scaling zones of every catchment ([[scaling.zones]]: regions split by distance to the "
                         "main divide) into <data>/rain/scaling_zones.npz (+ .json); scale: "
                         "gauge scale factors per zone (ECMWF at the gauge cells over 30 days into "
                         "<data>/rain/scaling/, copies of one station counted once, likely outages left out, "
                         "factors_ecmwf.json + history; re-scales the buffers); validate: one past storm, ECMWF raw "
                         "and gauge-scaled against the gauges and over catchments (--event; docs/rainfall-scaling.md) "
                         "into <data>/rain/validation/; totals: "
                         "upstream mm and m3 per reach and "
                         "window from the current buffers into <data>/rain/totals.npz (+ .json); inputs: the hourly "
                         "job's fixed inputs into --out (weights, network, scaling zones, viewer map, manifest); "
                         "publish: per-region rain files + index.json for the site into --out; status: config, "
                         "weight files, buffers and totals")
    pr.add_argument("--windows", type=int, nargs="+", default=None,
                    help="totals: windows in hours, any of 1..24 (default: windows in config/rainfall.toml)")
    pr.add_argument("--raw", action="store_true", help="totals: sum the raw values instead of the gauge-scaled ones")
    pr.add_argument("--rebuild-network", action="store_true",
                    help="totals: rebuild <data>/rain/network.npz from segments_table.pkl even if it looks current")
    pr.add_argument("--grid", choices=["gsmap", "ecmwf", "all"], default="all", help="weights: which grid(s)")
    pr.add_argument("--run", default="latest", help="ecmwf: 'latest' or a run YYYYMMDDHH (UTC, 00/06/12/18)")
    pr.add_argument("--obs", action="store_true", help="ecmwf: fill the observed buffer (ECMWF_OBS) only")
    pr.add_argument("--fc", action="store_true", help="ecmwf: fill the forecast buffer (ECMWF_FC) only")
    pr.add_argument("--reader", choices=["openmeteo-s3", "ecmwf-s3"], default=None,
                    help="ecmwf: where hourly IFS rain comes from (default: ecmwf.reader in config/rainfall.toml)")
    pr.add_argument("--now", default=None,
                    help="ecmwf, gauges, scale: treat this hour (YYYYMMDDHH, UTC) as now (default: the clock)")
    pr.add_argument("--force", action="store_true",
                    help="scale: a new factor version even within scaling.min_update_hours of the last one")
    pr.add_argument("--no-fetch", action="store_true",
                    help="scale: use the cell store as it is (no ECMWF downloads); validate: use the saved "
                         "event store")
    pr.add_argument("--days", type=int, default=None,
                    help="gauges: fetch the last N days of hours (default: gauges.days in config/rainfall.toml); "
                         "scale: the comparison window in days (default: scaling.days)")
    pr.add_argument("--event", nargs=2, metavar=("START", "END"), default=None,
                    help="validate: the storm, UTC hours YYYYMMDDHH (the rain in (START, END])")
    pr.add_argument("--region", nargs="+", default=["West Coast"],
                    help="validate: scaling zones or network regions to report (default: West Coast)")
    pr.add_argument("--catchment", nargs="+", default=None,
                    help="validate: outlet reaches, as river names (the largest reach of that name in the regions), "
                         "gids or NAME=GID (default for the West Coast: Haast, Waiho, Whataroa, Wanganui, Hokitika, "
                         "Arahura, Taramakau, Grey, Ahaura, Buller, Karamea)")
    pr.add_argument("--exclude-gauge", nargs="+", default=(), metavar="ID",
                    help="validate: gauge ids (council:site) left out of everything, factors included (a "
                         "sensitivity run)")
    pr.add_argument("--margin", type=int, default=12,
                    help="validate: ECMWF hours read on each side of the event, for the lags (default 12)")
    pr.add_argument("--council", nargs="+", default=None,
                    help="gauges: only these council ids (default: every enabled [gauges.councils.*])")
    pr.add_argument("--sites-only", action="store_true",
                    help="gauges: refresh the site list and placement (sites.json) without fetching data")
    pr.add_argument("--discover", action="store_true",
                    help="gauges: probe each council's candidate servers and files and print what they have "
                         "(<data>/rain/gauges/discover.json; default: the enabled councils); fetches no series")
    pr.add_argument("--refetch-hours", type=int, default=None,
                    help="gauges: fetch the newest N hours again even if cached (default: gauges.refetch_hours)")
    pr.add_argument("--no-cache", action="store_true", help="ecmwf: ignore and do not write the decoded-field cache")
    pr.add_argument("--retries", type=int, default=None,
                    help="ecmwf, gauges, scale, validate: retries per request on HTTP 429/5xx and network errors, "
                         "backoff doubling from ~1 s (default 5 for ecmwf, scale and validate, gauges.retries for "
                         "gauges)")
    pr.add_argument("-o", "--out", type=Path, default=None,
                    help="inputs: directory for the job's inputs (the rain-inputs branch); publish: directory for "
                         "index.json and <region>.bin.gz (the rain branch); validate: directory for the report "
                         "(default <data>/rain/validation)")
    pr.add_argument("--viewer", type=Path, default=None,
                    help="inputs: viewer data with regions_index.json (default: <data>/../viewer_nz)")
    pr.add_argument("--data", type=Path, default=None,
                    help="national data directory with tiles/ and segments_table.pkl (default: paths.nz_data in "
                         "config/rainfall.toml, i.e. data/nz)")
    pr.add_argument("--config", type=Path, default=None, help="rainfall config (default config/rainfall.toml)")
    pv = sub.add_parser("viewer", help="build the browser viewer for the given regions")
    pv.add_argument("regions", nargs="*")
    pv.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer")
    pv.add_argument("--max-px", type=int, default=1800)
    args = ap.parse_args(argv)
    if args.cmd == "nz":
        _nz(args)
        return
    if args.cmd == "models":
        _models(args)
        return
    if args.cmd == "external":
        _external(args)
        return
    if args.cmd == "web":
        from . import webapp

        if args.step == "data":
            from .linz import write_linz
            from .webdata import write_access, write_gauges, write_satellite, write_sections

            steps = {"sections": write_sections, "access": write_access, "gauges": write_gauges, "linz": write_linz,
                     "satellite": write_satellite}
            for k, step in steps.items():
                if not args.only or k in args.only:
                    step(args.data)
        elif args.step == "build":
            out = webapp.build(args.out, args.data if args.data.exists() else None, args.basemaps_key, args.rain_base)
            print(f"web -> {out['site']} and {out['artifact'].name} ({out['regions']} regions, "
                  f"{out['data_files']} data files)")
        elif args.step == "pages":
            import subprocess

            remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                                    text=True).stdout.strip()
            stage = args.out.parent / "web_pages"
            sha = webapp.deploy_pages(args.out, stage, remote, push=args.push)
            print(f"pages: staged {stage} as {sha}", "and pushed" if args.push else "(not pushed; add --push)")
        else:
            webapp.serve(args.out, args.port)
        return
    if args.cmd == "ml":
        _ml(args)
        return
    if args.cmd == "rain":
        _rain(args)
        return
    regions = [load_region(n) for n in (args.regions or list_regions())]

    if args.cmd == "viewer":
        from .viewer import build

        full, _ = build(regions, args.out, args.max_px)
        print(f"viewer -> {full} ({full.stat().st_size / 1e6:.1f} MB)")
        return
    if args.cmd == "score":
        _score(regions, args)
        return
    steps = list(STEPS) if args.cmd == "run" else [args.cmd]
    for r in regions:
        for s in steps:
            STEPS[s](r, args)
    if args.cmd == "run":
        _score(regions, args)
        main(["viewer", *[r.name for r in regions]])


if __name__ == "__main__":
    main()

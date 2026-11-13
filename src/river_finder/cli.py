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
    elif args.step == "score":
        from .nzscore import score_national

        score_national(tiles, log=log)
    else:
        from .viewer_nz import build

        log(f"viewer -> {build(args.out, page_only=args.page_only)}")


def _models(args):
    from . import models as M

    t0 = time.time()

    def log(m):
        print(f"[{(time.time() - t0) / 60:6.1f} min] {m}", flush=True)

    if args.step == "import":
        from .research import load_research, summary

        print(json.dumps(summary(load_research(args.labels)), indent=1, ensure_ascii=False))
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
        mapped = M.map_sections(table, M.all_sections(args.labels))
        card = M.train(table, mapped, M.load_split(args.split), args.name, args.title or args.name,
                       args.description or "", variant=args.variant)
        log(f"train -> {M.save_card(card)}: {card['labels']['positives']} positive reaches")
    elif args.step == "evaluate":
        M.evaluate_all(table, M.map_sections(table, M.all_sections(args.labels)), log=log)
    elif args.step == "viewer":
        from .viewer_nz import build

        M.write_viewer_scores(args.out, table, log=log)
        log(f"viewer page -> {build(args.out, page_only=True)}")


STEPS = {"fetch": _fetch, "route": _route, "validate": _validate, "reaches": _reaches, "export": _export}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="river-finder", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in [*STEPS, "score", "run"]:
        p = sub.add_parser(name, help="all steps + score + viewer" if name == "run" else f"{name} step")
        p.add_argument("regions", nargs="*", help=f"region names (default: all of {list_regions()})")
    pn = sub.add_parser("nz", help="national run: fetch | tiles | score | export | viewer (see docs/PLAN.md)")
    pn.add_argument("step", choices=["fetch", "tiles", "score", "viewer", "export"])
    pn.add_argument("tiles", nargs="*", help="tile ids such as x11y16 (default: every land tile)")
    pn.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz")
    pn.add_argument("--page-only", action="store_true", help="viewer: re-render only the page, keep the region files")
    pm = sub.add_parser("models", help="model registry: table | checkpoint | baseline | import | split | "
                                       "train | evaluate | viewer (see src/river_finder/models.py)")
    pm.add_argument("step", choices=["table", "checkpoint", "baseline", "import", "split", "train", "evaluate",
                                         "viewer"])
    pm.add_argument("--labels", type=Path, default=None, help="research JSON (default labels/guide_research.json)")
    pm.add_argument("--name", help="split name (split) or new model id (train)")
    pm.add_argument("--split", default="guides-v1", help="train: split to use")
    pm.add_argument("--variant", choices=["all", "sourced"], default="all",
                    help="train: 'sourced' drops sections whose coordinates were inferred")
    pm.add_argument("--title")
    pm.add_argument("--description")
    pm.add_argument("--seed", type=int, default=20261113)
    pm.add_argument("--test-frac", type=float, default=0.25)
    pm.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer_nz")
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

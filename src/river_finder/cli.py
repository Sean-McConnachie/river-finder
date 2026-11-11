"""Command-line entry point: ``river-finder <command> <region> ...``."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .regions import REPO_ROOT, list_regions, load_region


def _fetch(region, args):
    from . import rec2
    from .stac import mosaic

    t = time.time()
    p = mosaic(region.dem_coarse, region.bbox_wgs84, region.bbox_nztm(8.0), 8.0,
               region.data_dir() / "dem_8m.tif", REPO_ROOT / "data" / "_stac")
    print(f"[{region.name}] DEM -> {p} ({time.time() - t:.0f}s)")
    rec2.fetch_region(region.bbox_wgs84, region.data_dir())
    print(f"[{region.name}] REC2 reaches -> {region.data_dir()}")


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


def _export(region, args):
    from .export import export_region

    print(f"[{region.name}] GIS export -> {export_region(region)}")


STEPS = {"fetch": _fetch, "route": _route, "validate": _validate, "export": _export}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="river-finder", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in [*STEPS, "run"]:
        p = sub.add_parser(name, help="all steps + viewer" if name == "run" else f"{name} step")
        p.add_argument("regions", nargs="*", help=f"region names (default: all of {list_regions()})")
    pv = sub.add_parser("viewer", help="build the browser viewer for the given regions")
    pv.add_argument("regions", nargs="*")
    pv.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "data" / "viewer")
    pv.add_argument("--max-px", type=int, default=1800)
    args = ap.parse_args(argv)
    regions = [load_region(n) for n in (args.regions or list_regions())]

    if args.cmd == "viewer":
        from .viewer import build

        full, _ = build(regions, args.out, args.max_px)
        print(f"viewer -> {full} ({full.stat().st_size / 1e6:.1f} MB)")
        return
    steps = list(STEPS) if args.cmd == "run" else [args.cmd]
    for r in regions:
        for s in steps:
            STEPS[s](r, args)
    if args.cmd == "run":
        main(["viewer", *[r.name for r in regions]])


if __name__ == "__main__":
    main()

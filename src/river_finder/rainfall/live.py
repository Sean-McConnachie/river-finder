"""Live rain (rf-t26.9): the fixed inputs of the hourly job and the per-region files it publishes.

The job (``.github/workflows/rain.yml``, ``docs/live-rain.md``) runs every hour on GitHub Actions:
it checks out the ``rain-inputs`` branch (what ``river-finder rain inputs`` writes), runs ``rain
ecmwf``, ``rain totals`` and ``rain publish`` against it, and force-pushes the published files as one
orphan commit to the ``rain`` branch. The site reads them from
``https://raw.githubusercontent.com/<owner>/<repo>/rain/<file>`` (CORS ``*``, cached 5 min).

Stateless: ``rain ecmwf`` rebuilds the observed window (the 24 h up to "now", each hour from the
newest IFS run that has it: leads 1-6 of the runs before the latest, the latest's own leads up to
now) and the next 24 h (newest run covering them) from scratch in a fresh directory, so nothing
persists between runs; only the inputs below are fixed.

Inputs: ``river-finder rain inputs --out DIR``
----------------------------------------------
``DIR`` takes the same layout as a national data directory, so the job passes ``--data DIR``:

- ``rain/weights_ecmwf.npz`` (+ ``.json``): the grid-cell -> local-catchment weights (~18 MB);
- ``rain/network.npz``: the reach network (:class:`.upstream.Network`, ~17 MB), copied when it is
  current (same rows as the weights, built after ``segments_table.pkl`` last changed), else built
  from the table straight into ``DIR``. With it the job needs no segment table;
- ``rain/viewer.json``: the site's regions (``regions_index.json`` of the viewer data) mapped to
  network regions: ``{id, title, name, n}``. The rows of region ``name`` in viewer (``vi``) order
  are ``network.vi`` / ``region_of`` (:meth:`.upstream.UpstreamTotals.viewer_rows`); ``n`` must equal
  the site's reach count. The three 5 m reference areas (kaimai, karangahake, tutamoe) are listed
  under ``omitted`` (:data:`REFERENCE_NOTE`);
- ``manifest.json``: files with bytes and sha256, the national build (segment table time, weights
  and network build times, viewer index time), row digests, config values and code commit.

The config (``config/rainfall.toml``: windows, grid, reader) comes from the job's code checkout; the
manifest records the values the inputs were built with; ``rain inputs`` and ``rain ecmwf`` refuse
weights whose grid differs from the config's.

Published files: ``river-finder rain publish --out DIR`` (the contract for the map, rf-t26.10)
------------------------------------------------------------------------------------------------
``index.json`` (schema 1)::

    {"schema": 1, "format": "river-finder live rain v1",
     "generated": "2026-11-20T05:24:10Z",            # when the files were written (UTC)
     "now": "2026-11-20 05:00Z",                     # the whole hour the job ran in; forecasts start here
     "windows": [1, 3, 6, 12, 24],                   # hours
     "observed": {"window_end": "...", "hours_behind_now": 0, "runs": ["2026-11-19 06:00Z", ...],
                  "products": ["ECMWF_OBS"], "sources": ["openmeteo-s3"], "scaled": false, "scale": "raw",
                  "windows": {"24": {"start", "end", "hours_with_data", "missing_hours", "products", "runs",
                                     "leads"}, ...}},  # window x = (end - x h, end]
     "forecast": {"start": "...", "run": "2026-11-20 00:00Z", "run_age_hours": 5.4, "windows": {...}, ...},
     "ecmwf": {"reader": "openmeteo-s3", "runs": [{"run", "age_hours", "used_for"}], "newest_run",
               "newest_run_age_hours"},          # ages: generated - run, hours
     "units": {...}, "products": {"ECMWF_OBS": "...", ...}, "flags": {"1": {"name", "doc"}, ...},
     "attribution": "...", "licences": [{"name", "licence", "url"}],
     "regions": [{"id": "canterbury", "title": "Canterbury", "file": "canterbury.bin.gz", "n": 109254,
                  "bytes": 462243, "raw_bytes": 2516554, "sha256": "..."}],   # bytes: the .gz file
     "omitted": {"kaimai": "..."}, "totals": {"created", "rows", "rows_digest", "scaled", "values"},
     "summary": "...", "inputs": {...}, "job": {...}}

``observed.window_end`` is normally ``now`` (the forecast starts at the next hour); it lags ``now`` by
``hours_behind_now`` only when no ECMWF run has published the newest hours yet. ``scaled`` (per kind
and under ``totals``) is true only when some value used gauge scale factors (rf-t26.5, factor version
> 0); ``scale`` is ``raw`` (no factors yet), ``scaled``, ``mixed`` or ``none``, as the per-reach
``<p>_scale_<x>h`` bits below; ``totals.values`` says which array was summed (``raw`` after
``rain totals --raw``).

``<region id>.bin.gz``: one gzip member (``mtime`` 0) holding a little-endian binary file:

======  ========  =======================================================================
offset  type      content
======  ========  =======================================================================
0       4 bytes   magic ``RFR1``
4       uint32    ``H``: length of the header (UTF-8 JSON)
8       uint32    ``D``: offset of the data block (``12 + H`` rounded up to 8)
12      H bytes   header JSON, then zeros up to ``D``
D       ...       columns, ``n`` values each, at ``D + field.off`` (aligned to their size)
======  ========  =======================================================================

The header repeats ``schema``, ``region``, ``n``, ``generated``, ``now``, ``windows`` and the run times,
and lists ``fields``, in the vocabulary of the viewer's ``segHead`` (``viewer_nz.pack_segments``):
``{"k", "kind", "off", "scale", "add"}`` plus ``"of"`` (observed | forecast) and ``"x"`` (window, h).
A field whose value is the same for every reach has no column: it carries ``"const"`` (the decoded
value) instead of ``"off"``. Kinds: ``u16log`` = ``10 ** (q * scale + add)``; ``u16null`` = null
when ``q == 0``, else ``(q - 1) * scale + add``; ``u8`` = ``q``. Element ``j`` of every column is the
region's viewer reach ``j`` (``vi``), so ``n`` must equal the site's reach count (``segHead.n``);
if it does not, the rain inputs predate the site's data and the file must not be drawn.

Fields (``<p>`` = ``obs`` | ``fc``, ``<x>`` = each window):

- ``area_km2`` (u16log, scale 2e-4, add -5; 0.05 % steps from 1e-5 km²): upstream pixel area the
  depths are averaged over. Volume: ``m3 = mm / 1000 * area_km2 * 1e6`` (``= mm * area_km2 * 1000``).
- ``<p>_mm_<x>h`` (u16null, scale 0.1): upstream rain, mm (mean depth over the reach's whole upstream
  catchment), past ``x`` h (``obs``, ending ``observed.window_end``) or next ``x`` h (``fc``, from
  ``forecast.start``); 0.1 mm steps, to 6553.4 mm; null = no value (e.g. no forecast this hour).
- ``flags`` (u8 bits): 1 outlet, 2 tile-edge outlet (the totals stop there), 4 tile-edge outlet over
  200 km², 8 drains to a tile-edge outlet, 16 area short (the upstream pixel area is >= 5 % and
  >= 1 km² short of the reach's catchment area: the total misses part of the catchment).
- ``<p>_miss_<x>h`` (u8): most hours missing upstream (0 = complete); hours with no value count 0 mm.
- ``<p>_prod_<x>h`` (u8 bits over the header's ``products[<p>]`` list): products that supplied the window.
- ``<p>_scale_<x>h`` (u8 bits): 1 raw (unscaled), 2 gauge-scaled (rf-t26.5), 3 mixed, 0 no value.

Decoding (browser)::

    let b = new Uint8Array(await (await fetch(url)).arrayBuffer());
    if (b[0] === 0x1f && b[1] === 0x8b)                       // not decompressed in transit
      b = new Uint8Array(await new Response(new Blob([b]).stream()
            .pipeThrough(new DecompressionStream("gzip"))).arrayBuffer());
    const v = new DataView(b.buffer), H = v.getUint32(4, true), D = v.getUint32(8, true);
    const head = JSON.parse(new TextDecoder().decode(b.subarray(12, 12 + H)));
    // field f: "const" in f ? f.const : new Uint16Array(b.buffer, D + f.off, head.n) (u16*) or Uint8Array

:func:`decode_region` is the reference decoder (tests, ``docs/live-rain.md``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .config import RainConfig
from .grid import GridSpec
from .sources import PRODUCTS
from .upstream import FLAG_DOC, FLAG_NAMES, KINDS, PREFIX, Network, UpstreamTotals, network_from_files

SCHEMA = 1
FORMAT = "river-finder live rain v1"
INPUTS_FORMAT = "river-finder rain inputs v1"
VIEWER_FORMAT = "river-finder rain viewer map v1"
MAGIC = b"RFR1"
MM_SCALE = 0.1
AREA_SCALE, AREA_ADD = 2e-4, -5.0  # 10 ** (q * 2e-4 - 5): 1e-5 .. 1.2e8 km2 in 0.05 % steps
MANIFEST = "manifest.json"
VIEWER_MAP = "viewer.json"
REGION_SUFFIX = ".bin.gz"
REFERENCE_NOTE = ("5 m reference area with its own reach network: no clean reach-for-reach link to the national "
                  "reaches (2026-11-20: only 82-87 % of its viewer reaches lie within 150 m of a national reach, "
                  "and 80-87 % of those have an upstream area within 25 %); its national region has the rain")
ECMWF_LICENCE = {"name": "ECMWF IFS open data", "licence": "CC BY 4.0",
                 "url": "https://www.ecmwf.int/en/forecasts/datasets/open-data"}
OPENMETEO_LICENCE = {"name": "Open-Meteo (hourly copy of the ECMWF IFS run)", "licence": "CC BY 4.0",
                     "url": "https://open-meteo.com/"}
UNITS = {
    "mm": "upstream rain: mean depth over the reach's whole upstream catchment, mm (0.1 mm steps)",
    "area_km2": "upstream pixel area the depths are averaged over, km2",
    "m3": "mm / 1000 * area_km2 * 1e6",
    "time": "UTC; an hour is named by its end; window x observed = (window_end - x h, window_end], "
            "forecast = (start, start + x h]",
    "age_hours": "generated - run time, hours",
}


def _slug(name: str) -> str:
    """Region name -> site id (as ``nzscore.slug``: "Hawke's Bay" -> "hawkes-bay")."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")).strip("-")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime(p: Path) -> str:
    return _iso(datetime.fromtimestamp(p.stat().st_mtime, UTC))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _code_commit() -> str | None:
    from ..regions import REPO_ROOT

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _hour_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%MZ").replace(tzinfo=UTC)


def _write_json(path: Path, obj, indent=1) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False))
    os.replace(tmp, path)


# --- inputs -------------------------------------------------------------------------------------
def _network_for_inputs(cfg: RainConfig, wpath: Path, dst: Path, log) -> Network:
    """Copy ``network.npz`` if it is current, else build it from the segment table into ``dst``."""
    with np.load(wpath, allow_pickle=False) as z:
        w_digest = json.loads(str(z["meta"]))["rows_digest"]
    src, table = cfg.network_path, cfg.segments_path
    why = None
    if not src.exists():
        why = f"no {src}"
    else:
        net = Network.load(src)
        if net.rows_digest != w_digest:
            why = f"{src.name} rows differ from {wpath.name}"
        elif table.exists():
            st = table.stat()
            if (net.meta.get("table_size"), net.meta.get("table_mtime")) != (st.st_size, st.st_mtime):
                why = f"{table.name} changed since {src.name} was built"
    if why is None:
        shutil.copyfile(src, dst)
        log(f"network: {src} is current ({net.n} reaches, built {net.meta.get('built')}): copied")
        return net
    if not table.exists():
        raise SystemExit(f"{why}, and no {table} to build it from")
    log(f"network: building from {table.name} into {dst} ({why})")
    net = network_from_files(wpath, table, cfg.nz_data / "relink_log.csv", cfg.nz_data / "unresolved_gt200.csv",
                             log=log)
    if net.rows_digest != w_digest:
        raise SystemExit(f"{table} rows differ from {wpath}: rebuild the weights (river-finder rain weights)")
    net.save(dst)
    return net


def viewer_map(net: Network, index: list[dict] | None, reference: set[str] = frozenset()) -> dict:
    """The site's regions mapped to network regions, with reach counts checked (``rain/viewer.json``)."""
    counts = {name: int(np.count_nonzero((net.region_of == k) & (net.vi >= 0))) for k, name in enumerate(net.regions)}
    by_id = {_slug(name): name for name in net.regions}
    regions, omitted = [], {}
    if index is None:
        regions = [{"id": _slug(n), "title": n, "name": n, "n": counts[n]} for n in net.regions]
    else:
        for e in index:
            rid = e["id"]
            if rid in reference or str(e.get("group", "")).lower().startswith("reference"):
                omitted[rid] = REFERENCE_NOTE
                continue
            name = by_id.get(rid)
            if name is None:
                omitted[rid] = "not a region of the national reach network"
                continue
            n = counts[name]
            if e.get("segments") is not None and int(e["segments"]) != n:
                raise SystemExit(f"region {rid}: the site draws {e['segments']} reaches, the network has {n} "
                                 f"viewer reaches (vi >= 0): the viewer data and network.npz come from different "
                                 f"national builds")
            regions.append({"id": rid, "title": e.get("title", name), "name": name, "n": n})
    return {"format": VIEWER_FORMAT, "rows_digest": net.rows_digest, "regions": regions, "omitted": omitted,
            "rows": "network.npz: the rows of region <name> with vi >= 0, sorted by vi (UpstreamTotals.viewer_rows)"}


def build_inputs(cfg: RainConfig, out_dir: Path, viewer_dir: Path | None = None, log=print) -> dict:
    """Write the hourly job's fixed inputs into ``out_dir`` (layout of a data directory) and its manifest."""
    from ..regions import list_regions

    out_dir = Path(out_dir)
    if out_dir.resolve() == cfg.nz_data.resolve():
        raise SystemExit(f"--out {out_dir} is the data directory itself: give a new directory")
    rain = out_dir / cfg.rain
    rain.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    fams = sorted({cfg.observed_source, cfg.forecast_source})
    weights = {}
    for fam in fams:
        src = cfg.weights_path(fam)
        if not src.exists():
            raise SystemExit(f"no {src} (river-finder rain weights --grid {fam})")
        for p in (src, src.with_suffix(".json")):
            if p.exists():
                shutil.copyfile(p, rain / p.name)
                files.append(rain / p.name)
        with np.load(src, allow_pickle=False) as z:
            m = json.loads(str(z["meta"]))
        if not GridSpec.from_dict(m["grid"]).same_cells(cfg.grids[fam]):
            raise SystemExit(f"{src} is for another grid than [grids.{fam}] in the config: rebuild the weights "
                             f"(river-finder rain weights --grid {fam}), or the job fails at every ingest")
        weights[fam] = {"built": m.get("built"), "rows_digest": m.get("rows_digest"), "grid": m.get("grid")}
        log(f"weights {fam}: {src} ({src.stat().st_size / 1e6:.1f} MB), built {m.get('built')}")
    net_path = rain / cfg.network_path.name
    net = _network_for_inputs(cfg, cfg.weights_path(fams[0]), net_path, log)
    files.append(net_path)
    vdir = Path(viewer_dir) if viewer_dir else cfg.nz_data.parent / "viewer_nz"
    vindex = vdir / "regions_index.json"
    index = None
    if vindex.exists():
        index = json.loads(vindex.read_text())
    else:
        log(f"warning: no {vindex}: publishing every network region under its slug, without the site's checks")
    vm = viewer_map(net, index, set(list_regions()))
    vm["source"] = {"file": vindex.name, "mtime": _mtime(vindex)} if index is not None else None
    _write_json(rain / VIEWER_MAP, vm)
    files.append(rain / VIEWER_MAP)
    for r in vm["regions"]:
        log(f"  {r['id']:<15} {r['name']:<15} {r['n']:>7} viewer reaches")
    for rid, why in vm["omitted"].items():
        log(f"  {rid:<15} omitted: {why.split(':')[0]}")
    table = cfg.segments_path
    listing = [{"path": p.relative_to(out_dir).as_posix(), "bytes": p.stat().st_size, "sha256": _sha256(p)}
               for p in files]
    total = sum(f["bytes"] for f in listing)
    manifest = {
        "format": INPUTS_FORMAT, "created": _iso(datetime.now(UTC)), "code": _code_commit(),
        "national_build": {   # file names only: the branch is public, local paths stay local
            "segments_table": {"file": table.name, "mtime": _mtime(table), "bytes": table.stat().st_size}
            if table.exists() else None,
            "weights": weights, "network_built": net.meta.get("built"),
            "viewer_index": vm["source"]},
        "rows": net.n, "rows_digest": net.rows_digest, "down_digest": net.down_digest,
        "network_flags": net.meta.get("flags") or net.flag_counts(),
        "config": {"windows": list(cfg.windows), "observed": cfg.observed_source, "forecast": cfg.forecast_source,
                   "grids": {f: cfg.grids[f].to_dict() for f in fams}, "ecmwf_reader": cfg.ecmwf.reader,
                   "obs_leads": list(cfg.ecmwf.obs_leads), "obs_runs": cfg.ecmwf.obs_runs},
        "regions": [r["id"] for r in vm["regions"]], "omitted": vm["omitted"],
        "files": listing, "total_bytes": total,
    }
    _write_json(out_dir / MANIFEST, manifest)
    log(f"inputs -> {out_dir}: {len(listing)} files, {total / 1e6:.1f} MB; {len(vm['regions'])} regions, "
        f"{len(vm['omitted'])} omitted")
    return manifest


# --- region files -------------------------------------------------------------------------------
def quantize_mm(mm: np.ndarray) -> np.ndarray:
    """mm -> u16null at 0.1 mm (0 = no value, else 1 + round(mm / 0.1), capped at 6553.4 mm)."""
    v = np.asarray(mm, dtype=np.float64)
    ok = np.isfinite(v)
    q = np.clip(np.rint(np.where(ok, np.maximum(v, 0.0), 0.0) / MM_SCALE), 0, 65534) + 1
    return np.where(ok, q, 0).astype("<u2")


def quantize_area(km2: np.ndarray) -> np.ndarray:
    """km² -> u16log (``10 ** (q * 2e-4 - 5)``, the viewer's ``a`` kind); areas below 1e-5 km² clip to it."""
    x = np.log10(np.maximum(np.asarray(km2, dtype=np.float64), 10 ** AREA_ADD))
    return np.clip(np.rint((x - AREA_ADD) / AREA_SCALE), 0, 65535).astype("<u2")


def _decode(f: dict, q: np.ndarray) -> np.ndarray:
    if f["kind"] == "u16log":
        return 10 ** (q.astype(np.float64) * f["scale"] + f["add"])
    if f["kind"] == "u16null":
        return np.where(q == 0, np.nan, (q.astype(np.float64) - 1) * f["scale"] + f["add"])
    return q


def _const(f: dict, q0) -> float | int | None:
    v = _decode(f, np.asarray([q0]))[0]
    if f["kind"] == "u8":
        return int(v)
    return None if not np.isfinite(v) else round(float(v), 6)


def encode_region(header: dict, columns: list[tuple[dict, np.ndarray]]) -> bytes:
    """Pack ``columns`` (field spec, quantized array; u16 columns first) after ``header`` (see the module doc).

    A column whose values are all equal is stored as ``const`` in its field spec."""
    n = int(header["n"])
    fields, blobs, off = [], [], 0
    for spec, q in columns:
        q = np.ascontiguousarray(q)
        if q.shape != (n,):
            raise ValueError(f"{spec['k']}: {q.shape} values for {n} reaches")
        f = dict(spec)
        if n and (q == q[0]).all():
            f["const"] = _const(f, q[0])
        else:
            size = q.dtype.itemsize
            pad = (-off) % size
            blobs.append(b"\0" * pad)
            off += pad
            f["off"] = off
            blobs.append(q.astype(q.dtype.newbyteorder("<")).tobytes())
            off += q.nbytes
        fields.append(f)
    head = json.dumps(dict(header, fields=fields), separators=(",", ":"), ensure_ascii=False).encode()
    d = -(-(12 + len(head)) // 8) * 8
    pre = MAGIC + np.array([len(head), d], dtype="<u4").tobytes() + head
    return pre + b"\0" * (d - len(pre)) + b"".join(blobs)


def decode_region(blob: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """Reference decoder: (header, {field: values decoded, one per reach}); accepts the gzip file too."""
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    if blob[:4] != MAGIC:
        raise ValueError("not a river-finder rain region file")
    h, d = np.frombuffer(blob, dtype="<u4", count=2, offset=4)
    head = json.loads(blob[12:12 + int(h)].decode())
    n, out = int(head["n"]), {}
    for f in head["fields"]:
        if "const" in f:
            c = f["const"]
            out[f["k"]] = np.full(n, np.nan if c is None else c, dtype=np.uint8 if f["kind"] == "u8" else np.float64)
            continue
        dt = "<u1" if f["kind"] == "u8" else "<u2"
        q = np.frombuffer(blob, dtype=dt, count=n, offset=int(d) + int(f["off"]))
        out[f["k"]] = _decode(f, q)
    return head, out


def region_columns(t: UpstreamTotals, rows: np.ndarray) -> list[tuple[dict, np.ndarray]]:
    """The fields of one region's file, in file order (u16 first), for the reaches ``rows`` (vi order)."""
    rows = np.asarray(rows, dtype=np.int64)
    n = rows.size
    cols = [({"k": "area_km2", "kind": "u16log", "scale": AREA_SCALE, "add": AREA_ADD},
             quantize_area(t.up_area_m2[rows] / 1e6))]
    for kind in KINDS:
        k = t.kinds.get(kind)
        for x in t.windows:
            q = quantize_mm(k.mm[k.index(x), rows]) if k is not None else np.zeros(n, "<u2")
            cols.append(({"k": f"{PREFIX[kind]}_mm_{x}h", "kind": "u16null", "scale": MM_SCALE, "add": 0.0,
                          "of": kind, "x": x}, q))
    cols.append(({"k": "flags", "kind": "u8"}, t.flags[rows].astype(np.uint8)))
    for kind in KINDS:
        k = t.kinds.get(kind)
        for name, attr in (("miss", "miss_hours"), ("prod", "products"), ("scale", "scale")):
            for x in t.windows:
                q = getattr(k, attr)[k.index(x), rows].astype(np.uint8) if k is not None else np.zeros(n, np.uint8)
                cols.append(({"k": f"{PREFIX[kind]}_{name}_{x}h", "kind": "u8", "of": kind, "x": x}, q))
    return cols


# --- index --------------------------------------------------------------------------------------
def _kind_summary(t: UpstreamTotals, kind: str, generated: datetime) -> dict | None:
    k = t.kinds.get(kind)
    if k is None:
        return None
    i = k.info
    keep = ("start", "end", "hours_with_data", "missing_hours", "products", "runs", "leads", "reaches_incomplete")
    out = {"products": list(k.product_tags), "sources": i.get("sources", []), "runs": i.get("runs", []),
           "family": i.get("family"), "scaled": bool(i.get("scaled")), "scale": i.get("scale"),
           "windows": {x: {f: w.get(f) for f in keep} for x, w in i.get("windows_info", {}).items()}}
    if kind == "observed":
        out.update(window_end=i.get("window_end"), hours_behind_now=i.get("hours_behind_now"))
    else:
        run = i.get("run")
        out.update(start=i.get("start"), run=run,
                   run_age_hours=round((generated - _hour_dt(run)).total_seconds() / 3600, 1) if run else None)
    return out


def _ecmwf_runs(obs: dict | None, fc: dict | None, generated: datetime) -> dict:
    used: dict[str, list[str]] = {}
    for kind, s in (("observed", obs), ("forecast", fc)):
        for r in (s or {}).get("runs", []):
            used.setdefault(r, []).append(kind)
    runs = [{"run": r, "age_hours": round((generated - _hour_dt(r)).total_seconds() / 3600, 1), "used_for": u}
            for r, u in sorted(used.items())]
    readers = sorted({src for s in (obs, fc) if s for src in s.get("sources", [])})
    return {"reader": readers[0] if len(readers) == 1 else readers, "runs": runs,
            "newest_run": runs[-1]["run"] if runs else None,
            "newest_run_age_hours": runs[-1]["age_hours"] if runs else None}


def attribution(sources) -> tuple[str, list[dict]]:
    lic = [ECMWF_LICENCE]
    text = ("Rain: ECMWF IFS open data, © ECMWF, CC BY 4.0 (www.ecmwf.int); modified: regridded and summed "
            "over the river catchments")
    if "openmeteo-s3" in set(sources):
        lic.append(OPENMETEO_LICENCE)
        text += "; hourly fields via Open-Meteo (open-meteo.com, CC BY 4.0)"
    return text + ".", lic


def _summary(index: dict) -> str:
    o, f = index["observed"], index["forecast"]
    parts = [f"Rain {index['now']}"]
    if o:
        parts.append(f"observed to {o['window_end']} from ECMWF runs {', '.join(o['runs'])}")
    if f:
        parts.append(f"forecast from run {f['run']} ({f['run_age_hours']:g} h old)")
    return "; ".join(parts) + f" [{index['ecmwf']['reader']}]"


def publish(cfg: RainConfig, out_dir: Path, now: datetime | None = None, log=print) -> dict:
    """Per-region files and ``index.json`` from the current ``totals.npz`` (see the module doc)."""
    path = cfg.totals_path
    if not path.exists():
        raise SystemExit(f"no {path}: run river-finder rain totals first")
    t = UpstreamTotals.load(path)
    if not t.kinds:
        raise SystemExit(f"{path} holds no totals")
    vpath = cfg.rain_dir / VIEWER_MAP
    if vpath.exists():
        vm = json.loads(vpath.read_text())
        if vm.get("rows_digest") not in (None, t.meta.get("rows_digest")):
            raise SystemExit(f"{vpath} was built for another network ({vm['rows_digest']}) than {path.name} "
                             f"({t.meta.get('rows_digest')}): rebuild the inputs (river-finder rain inputs)")
    else:
        vm = {"regions": [{"id": _slug(r), "title": r, "name": r} for r in t.regions], "omitted": {}}
        log(f"note: no {vpath}: publishing every network region under its slug")
    mpath = cfg.nz_data / MANIFEST
    manifest = json.loads(mpath.read_text()) if mpath.exists() else None
    generated = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    obs = _kind_summary(t, "observed", generated)
    fc = _kind_summary(t, "forecast", generated)
    sources = sorted({s for k in (obs, fc) if k for s in k["sources"]})
    attr, licences = attribution(sources)
    tags = [tag for k in (obs, fc) if k for tag in k["products"]]
    head_common = {"schema": SCHEMA, "format": FORMAT, "generated": _iso(generated), "now": t.meta.get("now"),
                   "windows": list(t.windows), "rows_digest": t.meta.get("rows_digest"),
                   "observed": {k: obs[k] for k in ("window_end", "hours_behind_now", "runs")} if obs else None,
                   "forecast": {k: fc[k] for k in ("start", "run", "run_age_hours")} if fc else None,
                   "products": {PREFIX[kind]: list(t.kinds[kind].product_tags) for kind in t.kinds},
                   "attribution": attr}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_by = t.viewer_rows()
    regions = []
    for r in vm["regions"]:
        rows = rows_by.get(r["name"])
        if rows is None:
            raise SystemExit(f"region {r['name']!r} of {vpath.name} is not in {path.name}")
        if r.get("n") is not None and rows.size != r["n"]:
            raise SystemExit(f"region {r['id']}: {rows.size} viewer reaches in {path.name}, {r['n']} in {vpath.name}")
        header = dict(head_common, region=r["id"], title=r["title"], n=int(rows.size))
        raw = encode_region(header, region_columns(t, rows))
        gz = gzip.compress(raw, compresslevel=9, mtime=0)
        name = f"{r['id']}{REGION_SUFFIX}"
        tmp = out_dir / (name + ".tmp")
        tmp.write_bytes(gz)
        os.replace(tmp, out_dir / name)
        regions.append({"id": r["id"], "title": r["title"], "file": name, "n": int(rows.size), "bytes": len(gz),
                        "raw_bytes": len(raw), "sha256": hashlib.sha256(gz).hexdigest()})
    index = {
        "schema": SCHEMA, "format": FORMAT, "generated": _iso(generated), "now": t.meta.get("now"),
        "windows": list(t.windows), "observed": obs, "forecast": fc, "ecmwf": _ecmwf_runs(obs, fc, generated),
        "units": UNITS, "products": {tag: PRODUCTS[tag].note for tag in dict.fromkeys(tags)},
        "flags": {str(b): {"name": name, "doc": FLAG_DOC[name]} for b, name in FLAG_NAMES.items()},
        "attribution": attr, "licences": licences, "regions": regions, "omitted": vm.get("omitted", {}),
        "totals": {"created": t.meta.get("created"), "rows": t.n, "rows_digest": t.meta.get("rows_digest"),
                   "scaled": bool(t.meta.get("scaled")), "values": t.meta.get("values")},
        "inputs": {k: manifest.get(k) for k in ("created", "code", "national_build", "rows_digest")}
        if manifest else None,
        "job": _job(),
    }
    index["summary"] = _summary(index)
    _write_json(out_dir / "index.json", index)
    tot, totz = sum(r["raw_bytes"] for r in regions), sum(r["bytes"] for r in regions)
    big = max(regions, key=lambda r: r["raw_bytes"]) if regions else None
    log(f"publish -> {out_dir}: {len(regions)} regions, {tot / 1e6:.1f} MB ({totz / 1e6:.2f} MB gzipped)"
        + (f"; largest {big['id']} {big['raw_bytes'] / 1e6:.2f} MB ({big['bytes'] / 1e6:.2f} MB gzipped)" if big
           else ""))
    log(index["summary"])
    return index


def _job() -> dict | None:
    """The GitHub Actions run that wrote the files (from its environment), else None."""
    e = os.environ
    if not e.get("GITHUB_RUN_ID"):
        return None
    url = f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e.get('GITHUB_REPOSITORY', '')}/actions/runs/" \
          f"{e['GITHUB_RUN_ID']}"
    return {"run": url, "commit": e.get("GITHUB_SHA"), "event": e.get("GITHUB_EVENT_NAME")}

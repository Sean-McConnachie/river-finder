"""LINZ Data Service (LDS) vector layers for the web app.

Needs an LDS API key in ``$LINZ_LDS_KEY``. It is sent as an ``Authorization: key ...`` header and is
never written to disk, logs or the built pages.

- NZ River Name Lines (Pilot), LDS layer 103632: NZ Topo50 river centrelines (and canals) with LINZ
  river names (``name``, ``macronated``), split into river sections (``river_section_id``). CC BY 4.0.
  https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/

Per viewer region, ``<vid>_linz.json`` (river-finder web data):
``{"names": [...], "types": [...], "q": 1e-05, "lines": [[type, name | -1, river_section_id, flat], ...]}``,
``flat`` = ``[x, y, dx, dy, ...]``: integer lon/lat steps of ``q`` degrees, the first point and then each
point minus the one before it (decode: running sums times ``q``). Files from before ``q`` existed hold
plain ``[lon, lat, ...]``. Raw features are cached per region in ``data/nz/linz/<vid>.gpkg``.

What the map gets (compact): river and canal lines, not drains. Named lines are merged per name and type
(labels need long lines). Unnamed headwater lines (Strahler order 1: a free end upstream) that drain less
than 2 km² are dropped: in Otago half the lines and 60 % of the points, 98 % of them under 1 km²
(the modelled channels start at 2 km²). The order comes from the LINZ network itself (stream_order),
walked from each network's outlet: the free end of its line with the largest catchment on the region's
area grid (``Region.area``, line_area_km2). The unnamed lines left are joined end to end where only two
lines meet. Lines are simplified by 4 m (named, canals) or 8 m (unnamed). Otago: 149 000 map lines and
47 MB before, 35 000 lines and 6.2 MB (2.3 MB gzipped) after.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import numpy as np

WFS = "https://data.linz.govt.nz/services/wfs"
RIVER_NAME_LINES = 103632
LAYER_URL = "https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/"
KEEP_TYPES = ("river_cl", "canal_cl")     # drop farm drains (drain_cl)
ATTRIBUTION = "LINZ NZ River Name Lines (Pilot), CC BY 4.0"


def have_key() -> bool:
    return bool(os.environ.get("LINZ_LDS_KEY"))


def _get(params: dict, tries: int = 5) -> dict:
    url = f"{WFS}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    key = os.environ.get("LINZ_LDS_KEY")
    if key:
        headers["Authorization"] = f"key {key}"
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 (network: retry, then re-raise without the URL)
            if k == tries - 1:
                layer = params.get("typeNames")
                raise RuntimeError(f"LDS request failed for {layer}: {type(e).__name__}") from None
            time.sleep(2 * (k + 1))
    return {}


def wfs_features(layer: int, bbox_nztm, page: int = 5000, log=print) -> list[dict]:
    """Every feature of an LDS layer inside an NZTM box (EPSG:2193 coordinates), paged."""
    x0, y0, x1, y1 = bbox_nztm
    out, start = [], 0
    while True:
        d = _get({"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": f"layer-{layer}",
                  "outputFormat": "application/json", "srsName": "EPSG:2193", "sortBy": "id",
                  "bbox": f"{x0},{y0},{x1},{y1},EPSG:2193", "count": page, "startIndex": start})
        feats = d.get("features", [])
        out.extend(feats)
        if len(feats) < page:
            return out
        start += page
        log(f"  layer {layer}: {len(out)} features so far")


def river_lines(bbox_nztm, log=print) -> gpd.GeoDataFrame:
    feats = wfs_features(RIVER_NAME_LINES, bbox_nztm, log=log)
    if not feats:
        return gpd.GeoDataFrame({"feat_type": [], "name": [], "river_section_id": []}, geometry=[], crs=2193)
    g = gpd.GeoDataFrame.from_features(feats, crs="EPSG:2193")
    return g[["river_section_id", "feat_type", "name", "macronated", "geometry"]]


def _endpoints(geoms: np.ndarray, snap_m: float = 0.5) -> tuple[np.ndarray, np.ndarray, int]:
    """Start and end node of each line: endpoints that round to the same ``snap_m`` grid point are one node."""
    import shapely

    n = len(geoms)
    ends = np.vstack([shapely.get_coordinates(shapely.get_point(geoms, 0)),
                      shapely.get_coordinates(shapely.get_point(geoms, -1))])
    _, node = np.unique(np.round(ends / snap_m).astype(np.int64), axis=0, return_inverse=True)
    node = node.ravel()
    return node[:n], node[n:], int(node.max()) + 1 if n else 0


def stream_order(geoms, outlet_score) -> np.ndarray:
    """Strahler order of each line in the network formed by lines that share an endpoint.

    Each connected network drains to one outlet: the free end (an endpoint no other line shares) of its
    line with the highest ``outlet_score`` (catchment area along the line). The network is walked
    breadth first from there, so every line points away from the outlet; a headwater line (a free end
    upstream) is order 1, and two lines of order k meeting make order k + 1. A line closing a loop
    (braids) takes the lower order of its two ends, so order never drops downstream.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import breadth_first_order, connected_components

    geoms = np.asarray(geoms)
    n = len(geoms)
    if n == 0:
        return np.zeros(0, int)
    score = np.asarray(outlet_score, dtype=float)
    a, b, nn = _endpoints(geoms)
    deg = np.bincount(np.r_[a, b], minlength=nn)
    ncomp, comp = connected_components(coo_matrix((np.ones(n), (a, b)), shape=(nn, nn)), directed=False)
    # outlet: the free end of the best line with one; else (a closed loop) the network's first node
    roots = np.full(ncomp, -1)
    first = np.unique(comp, return_index=True)[1]
    roots[comp[first]] = first
    cand = np.flatnonzero((deg[a] == 1) | (deg[b] == 1))
    cand = cand[np.lexsort((-score[cand], comp[a[cand]]))]
    best = cand[np.r_[True, np.diff(comp[a[cand]]) != 0]]
    roots[comp[a[best]]] = np.where(deg[b[best]] == 1, b[best], a[best])
    # breadth first from a super-root joined to every outlet; pred[v] is the node v drains to
    sup = nn
    rows, cols = np.r_[a, np.full(ncomp, sup)], np.r_[b, roots]
    graph = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(nn + 1, nn + 1)).tocsr()
    bfs, pred = breadth_first_order(graph, sup, directed=False, return_predecessors=True)
    node_order = np.zeros(nn + 1, np.int64)
    top = np.zeros(nn + 1, np.int64)   # highest order draining into a node
    ntop = np.zeros(nn + 1, np.int64)  # how many lines of that order
    for v in bfs[::-1].tolist():
        if v == sup:
            continue
        t = top[v]
        o = 1 if t == 0 else t + 1 if ntop[v] >= 2 else t
        node_order[v] = o
        p = pred[v]
        if p != sup:
            if o > top[p]:
                top[p], ntop[p] = o, 1
            elif o == top[p]:
                ntop[p] += 1
    return np.minimum(node_order[a], node_order[b])


def load_area_grid(data_dir: Path, entry: dict) -> dict | None:
    """The region's catchment-area code grid (the map's ``Region.area``, see areacode.py), or None."""
    import base64
    import io

    from PIL import Image

    src = data_dir / entry.get("src", f"{entry['id']}.json")
    if not src.exists():
        return None
    meta = json.loads(src.read_text())
    area, merc = meta.get("area"), meta.get("merc")
    if not area or not merc:
        return None
    if area.startswith("data:"):
        img = Image.open(io.BytesIO(base64.b64decode(area.split(",", 1)[1])))
    elif (data_dir / area).exists():
        img = Image.open(data_dir / area)
    else:
        return None
    return {"code": np.asarray(img.convert("L")), "merc": [float(v) for v in merc]}


def line_area_km2(geoms, grid: dict, step_m: float = 25.0) -> np.ndarray:
    """Catchment area (km²) along each line (NZTM): the median of the area grid's cells every ``step_m``
    along it (0 off land). The grid's cells hold the largest area inside them (110 m in Otago), so
    a short line beside a big river can read that river's area."""
    import shapely
    from pyproj import Transformer

    from .areacode import decode_area

    geoms = np.asarray(geoms)
    A, (x0, y0, x1, y1) = grid["code"], grid["merc"]
    h, w = A.shape
    xy, idx = shapely.get_coordinates(shapely.segmentize(geoms, step_m), return_index=True)
    mx, my = Transformer.from_crs("EPSG:2193", "EPSG:3857", always_xy=True).transform(xy[:, 0], xy[:, 1])
    q = np.clip(np.floor((mx - x0) / (x1 - x0) * w), 0, w - 1).astype(np.int64)
    r = np.clip(np.floor((y1 - my) / (y1 - y0) * h), 0, h - 1).astype(np.int64)
    code = A[r, q].astype(np.int64)
    srt = np.lexsort((code, idx))
    cnt = np.bincount(idx, minlength=len(geoms))
    start = np.r_[0, np.cumsum(cnt)[:-1]]
    med = np.zeros(len(geoms), np.int64)
    has = cnt > 0
    med[has] = code[srt][start[has] + cnt[has] // 2]
    return np.nan_to_num(decode_area(med))


def _chains(a: np.ndarray, b: np.ndarray, deg: np.ndarray) -> list[list[tuple[int, bool]]]:
    """Lines joined end to end through nodes where only two lines meet (``deg``: lines per node, of all
    lines on the map): ``[(line, reversed), ...]`` per chain, in walking order."""
    at: dict[int, list[int]] = {}
    for i, (u, v) in enumerate(zip(a.tolist(), b.tolist(), strict=True)):
        at.setdefault(u, []).append(i)
        at.setdefault(v, []).append(i)
    done = np.zeros(len(a), bool)

    def walk(i: int, node: int) -> list[tuple[int, bool]]:
        out = []
        while deg[node] == 2:
            j = next((k for k in at[node] if k != i), -1)
            if j < 0 or done[j]:
                break
            done[j] = True
            rev = bool(b[j] == node)
            out.append((j, rev))
            i, node = j, (a[j] if rev else b[j])
        return out

    chains = []
    for i in range(len(a)):
        if not done[i]:
            done[i] = True
            back = walk(i, a[i])  # walked away from line i's start: reverse it to lead into line i
            chains.append([(j, not r) for j, r in reversed(back)] + [(i, False)] + walk(i, b[i]))
    return chains


def compact(g: gpd.GeoDataFrame, line_area=None, min_order: int = 2, keep_km2: float = 2.0,
            simplify_m: float = 4.0, small_simplify_m: float = 8.0, quantum: float = 1e-5) -> dict:
    """The map's JSON (module docstring) from raw LINZ lines (NZTM), with ``stats`` on what was kept.

    With ``line_area`` (NZTM lines -> catchment area in km² along each, e.g. ``line_area_km2`` on the
    region's area grid), unnamed river lines of Strahler order below ``min_order`` (stream_order) that
    drain less than ``keep_km2`` are dropped. Named lines (merged per name and type: labels need long
    lines) and canals are always kept; drains are always dropped. The unnamed lines left are joined end to
    end where no other line meets them. Lines are simplified by ``simplify_m`` (named, canals) or
    ``small_simplify_m`` (unnamed rivers), and coordinates are integer steps of ``quantum`` degrees.
    """
    import shapely
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)
    stats = {"raw": len(g), "drains": int((g["feat_type"] == "drain_cl").sum())}
    g = g[g["feat_type"].isin(KEEP_TYPES).to_numpy() & ~(g.geometry.isna() | g.geometry.is_empty).to_numpy()]
    g = g.explode(index_parts=False)
    g = g[(g.geom_type == "LineString").to_numpy()].reset_index(drop=True)
    g["name"] = g["name"].fillna("")
    if line_area is not None and len(g):
        geoms = g.geometry.to_numpy()
        km2 = np.asarray(line_area(geoms), dtype=float)
        order = stream_order(geoms, km2)
        unnamed = ((g["name"] == "") & (g["feat_type"] == "river_cl")).to_numpy()
        drop = unnamed & (order < min_order) & ~(km2 >= keep_km2)
        stats.update(headwater_dropped=int(drop.sum()), headwater_over_1km2=int((km2[drop] >= 1).sum()),
                     headwater_km=round(float(shapely.length(geoms[drop]).sum()) / 1e3))
        g = g[~drop].reset_index(drop=True)
    a, b, nn = _endpoints(g.geometry.to_numpy())
    deg = np.bincount(np.r_[a, b], minlength=nn)
    names: list[str] = []
    nidx: dict[str, int] = {}
    lines = []
    for (ftype, name), part in g.groupby(["feat_type", "name"], sort=False):
        sids = part["river_section_id"].astype(int).to_numpy()
        pg = part.geometry.to_numpy()
        if name:
            merged = shapely.line_merge(shapely.union_all(pg))
            geoms = [(x, int(sids[0])) for x in getattr(merged, "geoms", [merged])]
        else:
            pos = part.index.to_numpy()
            geoms = []
            for ch in _chains(a[pos], b[pos], deg):
                cs = [shapely.get_coordinates(pg[j])[::-1] if r else shapely.get_coordinates(pg[j]) for j, r in ch]
                geoms.append((shapely.LineString(np.vstack([cs[0]] + [c[1:] for c in cs[1:]])), int(sids[ch[0][0]])))
        tol = small_simplify_m if not name and ftype == "river_cl" else simplify_m
        for geom, sid in geoms:
            if geom is None or geom.is_empty or geom.geom_type != "LineString":
                continue
            x, y = to_ll.transform(*np.asarray(geom.simplify(tol).coords)[:, :2].T)
            if name and name not in nidx:
                nidx[name] = len(names)
                names.append(name)
            steps = np.round(np.column_stack([x, y]) / quantum).astype(np.int64)
            steps[1:] = np.diff(steps, axis=0)
            lines.append([KEEP_TYPES.index(ftype), nidx[name] if name else -1, sid, steps.ravel().tolist()])
    stats.update(lines=len(lines), named=sum(ln[1] >= 0 for ln in lines), points=sum(len(ln[3]) // 2 for ln in lines))
    return {"names": names, "types": list(KEEP_TYPES), "q": quantum, "lines": lines, "source": LAYER_URL,
            "attribution": ATTRIBUTION, "stats": stats}


def write_linz(data_dir: Path, refresh: bool = False, log=print, cache: Path | None = None) -> dict:
    """``<vid>_linz.json`` for every region in ``regions_index.json``, from the cached raw lines (fetched
    first only with ``refresh`` or when missing, which needs a key; skipped without one). The region's
    area grid (``Region.area``) gives each line's catchment for dropping headwater lines (compact)."""
    from pyproj import Transformer

    from .national import nz_dir

    cache = cache or nz_dir() / "linz"
    cache.mkdir(parents=True, exist_ok=True)
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
    counts = {}
    for e in json.loads((data_dir / "regions_index.json").read_text()):
        raw = cache / f"{e['id']}.gpkg"
        if refresh or not raw.exists():
            if not have_key():
                log(f"linz: {e['id']} skipped (set LINZ_LDS_KEY to fetch LINZ river lines)")
                continue
            (la0, lo0), (la1, lo1) = e["bounds"]
            g = river_lines(to_m.transform_bounds(lo0, la0, lo1, la1, densify_pts=21), log=log)
            g.to_file(raw, layer="rivers", driver="GPKG")
        g = gpd.read_file(raw)
        grid = load_area_grid(data_dir, e)
        if grid is None:
            log(f"linz: {e['id']} has no area grid, so every line is kept")
        out = compact(g, line_area=None if grid is None else (lambda geoms, grid=grid: line_area_km2(geoms, grid)))
        st = out.pop("stats")
        path = data_dir / f"{e['id']}_linz.json"
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        counts[e["id"]] = len(out["lines"])
        log(f"linz: {e['id']} {st['raw']} LINZ lines -> {st['lines']} map lines ({st['named']} named, "
            f"{st.get('headwater_dropped', 0)} headwater lines and {st['drains']} drains dropped), "
            f"{st['points']} points, {path.stat().st_size / 1e6:.1f} MB")
    return counts

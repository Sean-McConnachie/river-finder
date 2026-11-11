"""Flow routing on a raster DEM, treated as a graph.

Each cell is a vertex and has edges to its 8 neighbours. Pipeline:

1. ``priority_flood``: fill depressions to their spill level. It also records, for every cell,
   the neighbour the flood reached it from. On flats and in pits that neighbour gives a valid
   drainage direction toward the spill point (Barnes, Lehman & Mulla 2014, "improved"
   variant: a FIFO queue for cells inside depressions).
2. ``d8_receivers``: steepest-descent receiver on the filled DEM. If a cell has no strictly
   lower neighbour (flat), use the Priority-Flood receiver.
3. ``accumulate``: flow accumulation with Kahn's topological sort over the receiver forest.

Arrays are flattened row-major; ``rec[i] == -1`` means cell ``i`` is an outlet (it drains off the
grid or into the sea) or is nodata.

Why D8 + Priority-Flood receivers has no cycles: along any flow path, filled elevation never
rises. D8 steps strictly lower it. Flat steps keep it level but always move to a cell the flood
popped earlier. A cycle would need to come back to the same elevation, so it could only use
flat steps, and the pop order rules that out.
"""

from __future__ import annotations

import numpy as np
from numba import njit

# 8-neighbour offsets (row, col)
DR = np.array([-1, -1, -1, 0, 0, 1, 1, 1], dtype=np.int64)
DC = np.array([-1, 0, 1, -1, 1, -1, 0, 1], dtype=np.int64)
DIST = np.array([np.sqrt(2.0), 1.0, np.sqrt(2.0), 1.0, 1.0, np.sqrt(2.0), 1.0, np.sqrt(2.0)])


# ---------------------------------------------------------------------------
# Binary min-heap keyed on (elevation, insertion sequence). The sequence number breaks ties
# first-in-first-out, so floods spread breadth-first across flats and drainage looks natural.
# ---------------------------------------------------------------------------
@njit(cache=True)
def _less(hk, hs, a, b):
    return hk[a] < hk[b] or (hk[a] == hk[b] and hs[a] < hs[b])


@njit(cache=True)
def _swap(hk, hs, hv, a, b):
    hk[a], hk[b] = hk[b], hk[a]
    hs[a], hs[b] = hs[b], hs[a]
    hv[a], hv[b] = hv[b], hv[a]


@njit(cache=True)
def _heap_push(hk, hs, hv, size, key, seq, val):
    i = size
    hk[i] = key
    hs[i] = seq
    hv[i] = val
    while i > 0:
        p = (i - 1) >> 1
        if _less(hk, hs, i, p):
            _swap(hk, hs, hv, i, p)
            i = p
        else:
            break
    return size + 1


@njit(cache=True)
def _heap_pop(hk, hs, hv, size):
    top = hv[0]
    size -= 1
    hk[0] = hk[size]
    hs[0] = hs[size]
    hv[0] = hv[size]
    i = 0
    while True:
        left = 2 * i + 1
        if left >= size:
            break
        m = left
        right = left + 1
        if right < size and _less(hk, hs, right, left):
            m = right
        if _less(hk, hs, m, i):
            _swap(hk, hs, hv, m, i)
            i = m
        else:
            break
    return top, size


@njit(cache=True)
def priority_flood(z, valid):
    """Fill depressions and assign a drainage receiver to every valid cell.

    Parameters
    ----------
    z : 2-D float array of elevations.
    valid : 2-D bool array; False marks nodata (sea, outside coverage). Nodata cells act as
        outlets, as does the grid border.

    Returns
    -------
    filled : 2-D float64 array, ``z`` raised to spill level inside depressions.
    rec : 1-D int64 array of receivers (flattened index, -1 for outlets and nodata).
    order : 1-D int64 array of valid cells in the order the flood processed them (downstream
        first), length = number of valid cells.
    """
    nr, nc = z.shape
    n = nr * nc
    zf = z.ravel()
    vf = valid.ravel()
    filled = zf.astype(np.float64).copy()
    rec = np.full(n, -1, dtype=np.int64)
    closed = np.zeros(n, dtype=np.uint8)
    order = np.empty(n, dtype=np.int64)
    nvalid = 0

    hk = np.empty(n, dtype=np.float64)
    hs = np.empty(n, dtype=np.int64)
    hv = np.empty(n, dtype=np.int64)
    size = 0
    seq = 0
    pit = np.empty(n, dtype=np.int64)  # FIFO queue; each cell enters at most once
    ph = 0
    pt = 0

    # Seed: valid cells on the grid border or next to nodata.
    for i in range(n):
        if not vf[i]:
            closed[i] = 1
            continue
        nvalid += 1
        r = i // nc
        c = i - r * nc
        seed = r == 0 or c == 0 or r == nr - 1 or c == nc - 1
        if not seed:
            for k in range(8):
                j = (r + DR[k]) * nc + (c + DC[k])
                if not vf[j]:
                    seed = True
                    break
        if seed:
            closed[i] = 1
            size = _heap_push(hk, hs, hv, size, filled[i], seq, i)
            seq += 1

    k_out = 0
    while ph < pt or size > 0:
        if ph < pt:
            ci = pit[ph]
            ph += 1
        else:
            ci, size = _heap_pop(hk, hs, hv, size)
        order[k_out] = ci
        k_out += 1
        r = ci // nc
        c = ci - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if closed[j]:
                continue
            closed[j] = 1
            rec[j] = ci
            if filled[j] <= filled[ci]:
                filled[j] = filled[ci]
                pit[pt] = j
                pt += 1
            else:
                size = _heap_push(hk, hs, hv, size, filled[j], seq, j)
                seq += 1

    return filled.reshape(nr, nc), rec, order[:k_out]


@njit(cache=True)
def d8_receivers(filled, valid, pf_rec, cellsize):
    """Steepest-descent (D8) receivers on a filled DEM, with Priority-Flood receivers on flats."""
    nr, nc = filled.shape
    n = nr * nc
    zf = filled.ravel()
    vf = valid.ravel()
    rec = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        if not vf[i]:
            continue
        r = i // nc
        c = i - r * nc
        best = 0.0
        bj = -1
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if not vf[j]:
                continue
            s = (zf[i] - zf[j]) / (DIST[k] * cellsize)
            if s > best:
                best = s
                bj = j
        if bj >= 0:
            rec[i] = bj
        elif pf_rec[i] == -1:
            rec[i] = -1  # outlet
        else:
            rec[i] = -2  # flat: resolved by resolve_flats
    return rec


@njit(cache=True)
def resolve_flats(filled, valid, rec, pf_rec):
    """Drain flats toward their outlets *and* away from higher terrain (Barnes, Lehman & Mulla 2014b).

    Flats come from depression filling, and from contour-interpolated DEMs where whole valley
    floors sit at one contour level. Each flat cell gets a mask value
    ``2 * (steps to the low edge) + (steps from the high edge, inverted)``. Its receiver is the
    neighbour with the lowest mask. Flow therefore runs down the middle of a valley floor rather
    than along its sides. Cells marked -2 in ``rec`` are resolved in place.
    """
    nr, nc = filled.shape
    n = nr * nc
    zf = filled.ravel()
    vf = valid.ravel()
    flat = rec == -2
    q = np.empty(n, dtype=np.int64)

    # 1. Distance (BFS steps) from low edges: drained/outlet cells next to an equal-height flat cell.
    dlow = np.full(n, -1, dtype=np.int32)
    qh = 0
    qt = 0
    for i in range(n):
        if not vf[i] or flat[i]:
            continue
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if flat[j] and zf[j] == zf[i]:
                dlow[i] = 0
                q[qt] = i
                qt += 1
                break
    while qh < qt:
        v = q[qh]
        qh += 1
        r = v // nc
        c = v - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if flat[j] and dlow[j] < 0 and zf[j] == zf[v]:
                dlow[j] = dlow[v] + 1
                q[qt] = j
                qt += 1

    # 2. Distance from high edges: flat cells next to strictly higher terrain.
    dhigh = np.zeros(n, dtype=np.int32)
    qh = 0
    qt = 0
    for i in range(n):
        if not flat[i]:
            continue
        r = i // nc
        c = i - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if vf[j] and zf[j] > zf[i]:
                dhigh[i] = 1
                q[qt] = i
                qt += 1
                break
    while qh < qt:
        v = q[qh]
        qh += 1
        r = v // nc
        c = v - r * nc
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if flat[j] and dhigh[j] == 0 and zf[j] == zf[v]:
                dhigh[j] = dhigh[v] + 1
                q[qt] = j
                qt += 1

    # 3. Label flats; record the largest high-edge distance per flat.
    label = np.full(n, -1, dtype=np.int64)
    hmax = np.zeros(n, dtype=np.int32)
    nl = 0
    for s in range(n):
        if not flat[s] or label[s] >= 0:
            continue
        qh = 0
        qt = 1
        q[0] = s
        label[s] = nl
        m = dhigh[s]
        while qh < qt:
            v = q[qh]
            qh += 1
            if dhigh[v] > m:
                m = dhigh[v]
            r = v // nc
            c = v - r * nc
            for k in range(8):
                rr = r + DR[k]
                cc = c + DC[k]
                if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                    continue
                j = rr * nc + cc
                if flat[j] and label[j] < 0 and zf[j] == zf[v]:
                    label[j] = nl
                    q[qt] = j
                    qt += 1
        hmax[nl] = m
        nl += 1

    # 4. Combined mask and receivers.
    big = np.int64(1) << 40
    mask = np.full(n, big, dtype=np.int64)
    for i in range(n):
        if flat[i] and dlow[i] >= 0:
            mask[i] = 2 * np.int64(dlow[i])
            if dhigh[i] > 0:
                mask[i] += hmax[label[i]] - dhigh[i]
    for i in range(n):
        if not flat[i]:
            continue
        r = i // nc
        c = i - r * nc
        best = mask[i]
        bj = -1
        for k in range(8):
            rr = r + DR[k]
            cc = c + DC[k]
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            j = rr * nc + cc
            if not vf[j] or zf[j] != zf[i]:
                continue
            if flat[j]:
                m = mask[j]
            elif dlow[j] == 0:
                m = -1  # low edge: leaving the flat is always best
            else:
                continue
            if m < best:
                best = m
                bj = j
        rec[i] = bj if bj >= 0 else pf_rec[i]
    return rec


@njit(cache=True)
def accumulate(rec, weight, is_edge):
    """Weighted flow accumulation via Kahn's topological sort.

    Parameters
    ----------
    rec : 1-D int64 receivers (-1 = outlet / nodata).
    weight : 1-D float array, per-cell contribution (e.g. cell area, or area x rainfall).
    is_edge : 1-D bool array marking cells on the grid border. Any cell whose upstream area
        includes one of these may be missing area that lies outside the grid.

    Returns
    -------
    acc : 1-D float64 accumulated weight (includes the cell itself).
    truncated : 1-D bool, True where the upstream area touches the grid border.
    n_done : number of cells processed (equals rec.size when the graph has no cycles).
    """
    n = rec.size
    indeg = np.zeros(n, dtype=np.int32)
    for i in range(n):
        if rec[i] >= 0:
            indeg[rec[i]] += 1
    acc = weight.astype(np.float64).copy()
    trunc = is_edge.copy()
    queue = np.empty(n, dtype=np.int64)
    head = 0
    tail = 0
    for i in range(n):
        if indeg[i] == 0:
            queue[tail] = i
            tail += 1
    while head < tail:
        v = queue[head]
        head += 1
        r = rec[v]
        if r >= 0:
            acc[r] += acc[v]
            if trunc[v]:
                trunc[r] = True
            indeg[r] -= 1
            if indeg[r] == 0:
                queue[tail] = r
                tail += 1
    return acc, trunc, head


@njit(cache=True)
def upstream_mask(rec, outlet):
    """Boolean mask of every cell that drains through ``outlet`` (reverse BFS on the flow graph)."""
    n = rec.size
    # Build donor lists in CSR form.
    cnt = np.zeros(n + 1, dtype=np.int64)
    for i in range(n):
        if rec[i] >= 0:
            cnt[rec[i] + 1] += 1
    for i in range(n):
        cnt[i + 1] += cnt[i]
    donors = np.empty(cnt[n], dtype=np.int64)
    fillp = cnt[:-1].copy()
    for i in range(n):
        r = rec[i]
        if r >= 0:
            donors[fillp[r]] = i
            fillp[r] += 1
    mask = np.zeros(n, dtype=np.bool_)
    stack = np.empty(n, dtype=np.int64)
    sp = 0
    stack[sp] = outlet
    sp += 1
    mask[outlet] = True
    while sp > 0:
        sp -= 1
        v = stack[sp]
        for t in range(cnt[v], cnt[v + 1]):
            d = donors[t]
            if not mask[d]:
                mask[d] = True
                stack[sp] = d
                sp += 1
    return mask


def edge_mask(shape: tuple[int, int]) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[0, :] = m[-1, :] = m[:, 0] = m[:, -1] = True
    return m


def route(dem: np.ndarray, valid: np.ndarray, cellsize: float, weight: np.ndarray | None = None):
    """Run the full pipeline. Returns dict with filled, rec, area_m2, truncated (2-D where sensible).

    ``weight`` defaults to cell area in m^2; pass e.g. cell area x rainfall for a flow proxy.
    """
    dem = np.ascontiguousarray(dem, dtype=np.float64)
    valid = np.ascontiguousarray(valid, dtype=np.bool_)
    filled, pf_rec, order = priority_flood(dem, valid)
    rec = d8_receivers(filled, valid, pf_rec, float(cellsize))
    rec = resolve_flats(filled, valid, rec, pf_rec)
    if weight is None:
        w = np.where(valid, cellsize * cellsize, 0.0).ravel()
    else:
        w = np.where(valid, weight, 0.0).ravel().astype(np.float64)
    acc, trunc, n_done = accumulate(rec, w, edge_mask(dem.shape).ravel() & valid.ravel())
    if n_done != rec.size:
        raise RuntimeError(f"flow graph has a cycle: processed {n_done} of {rec.size} cells")
    shape = dem.shape
    return {
        "filled": filled,
        "rec": rec,
        "acc": np.where(valid, acc.reshape(shape), np.nan),
        "truncated": trunc.reshape(shape) & valid,
        "fill_depth": np.where(valid, filled - dem, np.nan),
    }

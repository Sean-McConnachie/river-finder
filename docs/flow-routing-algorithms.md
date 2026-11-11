# Flow routing as a graph problem

## The graph

Treat the DEM as a graph. Each raster cell is a **vertex** with elevation `z`. Each cell has
**edges** to its 8 neighbours (D8). The edge length is `1` or `√2` × the cell size.

Water obeys one rule: it moves to a lower neighbour. So the problem splits into three graph
questions.

| Question | Graph view | Algorithm |
|---|---|---|
| Where does water leave each cell? | Choose out-edge(s) per vertex. | Flow direction: D8, D∞, MFD |
| What if no neighbour is lower (pit, lake, flat plateau top)? | A local minimum. Find the **spill point**, the lowest saddle on the rim. | Depression handling: Priority-Flood, breaching, Fill-Spill-Merge |
| How much land drains through cell *v*? | Size (or weighted size) of *v*'s **upstream subtree**. | Flow accumulation by topological sort |

Your intuition ("for a set of vertices, this is the lowest point, so water must flow here") is a
local-minimum search. On real DEMs, most local minima are small noise pits. The useful version of
the question is: **"From this vertex, what is the lowest path to the sea, where 'lowest' means the
highest point you must climb over is as small as possible?"** This is the *minimax* (bottleneck)
path problem, and Priority-Flood solves it in one pass.

## 1. Depression handling — Priority-Flood (the core algorithm)

Barnes, Lehman & Mulla (2014), *Priority-Flood: An optimal depression-filling and
watershed-labeling algorithm for DEMs*, Computers & Geosciences 62:117–127.

```
open = min-priority-queue seeded with all edge cells (and sea/no-data cells) by elevation
closed = {seeds}
while open not empty:
    c = pop lowest
    for each neighbour n of c not in closed:
        closed += n
        z_filled[n] = max(z[n], z_filled[c])      # fill to spill level if n is in a pit
        dir[n] = c                                # water in n drains toward c
        push(n, z_filled[n])
```

- This is **Dijkstra's algorithm** where path cost is the *maximum* elevation along the path,
  not the sum. The result `z_filled[v]` is the minimax elevation from *v* to the boundary. This is
  exactly the water level at which *v* spills.
- It is the same as building a **minimum spanning tree** (Prim's algorithm) over the graph with
  edge weight `max(z_u, z_v)`. Depressions and spill points are the merge points of that tree.
- `dir[n] = c` gives a flow direction for every cell, including cells inside pits and on flats.
  This handles the flat plateau tops of our reference regions without extra work.
- Complexity is O(n log n). The "improved" variants use a plain FIFO queue for cells inside
  depressions, which gets close to O(n). Wang & Liu (2006) and Zhou et al. (2016) are other
  versions of the same idea.

**Fill vs breach.** Filling raises pit cells to their spill level. Breaching instead carves a
path down through the obstruction. Lindsay (2016, *Hydrological Processes*) shows breaching
changes the DEM less. This matters on LiDAR, where road embankments and culverts act as fake
dams. Plan: start with fill. Add least-cost breaching once we use 1 m LiDAR.

**Flats.** Filled areas are perfectly flat, so D8 has no gradient there. Barnes et al. (2014b),
*An efficient assignment of drainage direction over flat surfaces*, builds two BFS gradients:
one *away from* higher terrain and one *toward* the outlet. Priority-Flood's `dir[]` already
gives a valid but less natural drainage pattern, which is acceptable for a first version.

**Depression hierarchy (later).** Barnes, Callaghan & Wickert (2020/21), *Computing water flow
through complex landscapes* Parts 2 & 3, build a **merge tree** of nested depressions with
union-find. They then run *Fill-Spill-Merge*: with finite rain, a pit only spills once it is
full. This matters if we model plateau swamps or tarns that store water.

## 2. Flow direction — out-edges per vertex

| Method | Out-edges | Notes |
|---|---|---|
| **D8** — O'Callaghan & Mark (1984) | 1 (steepest of 8) | Flow graph is a **forest** of trees rooted at outlets. Simple and exact for channels. Makes parallel streaks on smooth hillslopes. **Start here.** |
| **D∞** — Tarboton (1997), WRR 33(2) | 1–2 (split by facet angle) | Better on open slopes. Flow graph is a DAG. |
| **MFD** — Freeman (1991); Quinn et al. (1991) | up to 8, weighted by slope^p | Spreads flow out. Good for wide, gently sloping plateau tops. |

Plan: use D8 for the channel network and catchment areas. Offer MFD/D∞ as an option to compare
on the plateau tops.

## 3. Flow accumulation — catchment area per vertex

With D8, every cell has one downstream receiver, so the drainage graph is a forest.
Accumulation `A[v] = w[v] + Σ A[u]` over all *u* draining into *v*. Compute it in O(n) with
**Kahn's topological sort**:

```
indeg[v] = number of cells draining into v
queue = all v with indeg 0                   # ridgelines / headwaters
A = w                                        # w = cell area, or cell area × rainfall
while queue:
    v = pop
    r = receiver[v]
    A[r] += A[v]
    indeg[r] -= 1
    if indeg[r] == 0: push r
```

- Use `w = cell_area` to get **catchment area** (the first heat map).
- Use `w = cell_area × mean_annual_rainfall × runoff_coeff` to get **mean-flow proxy** (m³/s).
  Rain on a plateau that drains into one gorge is our "plateau → river" hypothesis expressed as
  one number.
- For MFD/D∞, use the same algorithm but split `A[v]` across receivers by weight.
- Braun & Willett (2013) build the same "stack" order in O(n) for landscape-evolution models.
  This is useful if we need to recompute quickly while tuning parameters.

**Watershed of a point.** To get the catchment polygon of a chosen outlet, run BFS on the
**reversed** D8 graph from the outlet. This is the "what's upstream of this gorge" query.

## 4. From flow graph to "is it a kayak run?"

These are standard tectonic-geomorphology metrics. They fit our hypothesis because a volcanic
plateau incised by rivers produces knickpoints (waterfalls, gorges) where streams cut through
the hard caprock.

- **Stream extraction:** cells where `A > A_crit` (about 0.1–1 km²). Then Strahler/Shreve
  ordering on the tree.
- **Reach gradient (m/km):** smoothed elevation change along the D8 path over a 0.5–2 km window.
- **Channel steepness `k_sn = S · A^θ`** with θ ≈ 0.45: a slope measure normalised for catchment
  size. High `k_sn` marks gorges and knickzones whatever the river size. Use **χ-analysis**
  (Perron & Royden 2013, ESPL) to estimate `k_sn` robustly on noisy DEMs.
- **Knickpoint detection:** sharp breaks in the χ–z profile or in the gradient series. Validate
  against LINZ Topo50 waterfall points.
- **Gorge depth / confinement:** relative relief across the channel, or a valley-bottom width
  proxy from cross-section transects.

## 5. Libraries (for cross-checking our own implementation)

| Library | Language | Has |
|---|---|---|
| [RichDEM](https://richdem.readthedocs.io) (Barnes) | C++/Python | Priority-Flood, flats, D8/D∞/MFD, accumulation |
| [pysheds](https://github.com/mdbartos/pysheds) | Python | fill/breach, D8/D∞/MFD, accumulation, catchment, river network |
| [WhiteboxTools](https://www.whiteboxgeo.com) | Rust CLI/Python | Lindsay breaching, huge DEMs, many hydrology tools |
| [TopoToolbox](https://topotoolbox.wordpress.com) / LSDTopoTools | MATLAB/Python, C++ | χ, `k_sn`, knickpoints |
| GRASS `r.watershed` | C | A* least-cost routing, handles large rasters |

Plan: write our own Priority-Flood + D8 + Kahn accumulation in NumPy + Numba. It is short, and it
keeps us in control of weighting and tiling. Test it against RichDEM/pysheds output on the same
tiles.

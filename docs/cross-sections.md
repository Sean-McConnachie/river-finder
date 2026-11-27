# Cross-sections per reach (rf-zmy.10)

*The 1 m LiDAR version, its channel-shape features and the storage spec for the national rebuild:
[`cross-sections-1m.md`](cross-sections-1m.md) (rf-lxa.4).*

The question: does a reach's cross-section (how high and how narrow the channel is, as opposed to the valley-floor
width the model already has) add information? A constricted channel carries the same flood faster and deeper than a
wide one. `src/river_finder/xsec.py` measures DEM cross-sections along every reach, and `ml.py` turns them into
features (`xs_*`, group `xsec`) and a recipe `gbm_xsec` (= `gbm_terrain` + the cross-section features).

```bash
uv run river-finder nz xsec                       # every national tile with reaches (~65 min, < 2 GB; resumable)
                                                  # -> data/nz/xsec/tiles/<tile>.pkl + data/nz/xsec.pkl (key gid)
uv run river-finder nz xsec x12y16 x11y11         # some tiles only (the merged table keeps every tile file)
uv run river-finder xsec kaimai karangahake tutamoe [--dem 8m|5m|both]   # reference areas -> data/<region>/xsec_8m.pkl
uv run river-finder ml table                      # joins data/nz/xsec.pkl (a cached table also joins it on load)
uv run river-finder ml cv                         # the whole zoo, now with gbm_xsec and logit_xsec
```

`ml cv --models ...` writes `models/cv/results.json` and `data/nz/ml_oof.npz` with only the models named, so run
the whole zoo for the committed results. Without `data/nz/xsec.pkl`, `ml cv` leaves the two cross-section recipes
out and says so. A card with cross-section features scores a reference area from `data/<region>/xsec_8m.pkl`, so run
`river-finder xsec` for the three reference areas before `models viewer` or `web data`.

## Method

Per reach (the 250 m segments of `reaches.py`, every reach of 1 km² or more):

- **Sections** every 50 m along the reach line, centred (a 250 m reach gets sections at 25, 75, ..., 225 m; a reach
  shorter than 50 m gets one at its midpoint), perpendicular to the chord of the line from 40 m above to 40 m below
  the station (smooths the D8 zig-zag).
- **Profile**: the raw DEM (not the filled one) sampled bilinearly every half cell, 500 m out on each side.
- **Channel bottom** `z0`: the lowest sample within 16 m (two 8 m cells) of the line, because the routed line can
  sit a cell or two off the DEM's valley axis; the section is re-centred there. Where a DEM cell in the 3 x 3
  window around the station is lower, `z0` is that cell: on a diagonal D8 stretch a station can fall on a cell
  corner, where bilinear sampling averages two channel cells with two bank cells.
- **Stages** h = 1, 2, 5, 10 and 20 m above `z0`. On each side, walk out from the bottom until the ground first
  rises above `z0 + h`. Width `w(h)` is the sum of the two distances, flow area `A(h)` the area between the level
  and the ground (trapezoids), `P(h)` the wetted perimeter. Ground beyond a ridge never counts. A side that does
  not reach the level within 500 m is *censored* at 500 m (so widths are at most 1000 m). No-data samples (sea)
  count as flat ground at `z0`, so coastal and lake sections come out wide and censored.
- **Banks and walls**: on each side, the highest ground within 50 m (bank) and within 150 m (wall, the gorge
  radius of `reaches.py`) above `z0`; the lower and the higher side are both kept.
- **Per reach**: median and minimum width per stage, median area and wetted perimeter per stage, share of
  censored sections per stage, median bank and wall heights (lower and higher side), share of flat sections (both
  sides censored at 1 m: lakes, wide flats), no-data share and the DEM cell size `xs_res_m`.
- **Lakes and flats** are not special-cased: their sections are censored at the low stages and come out as
  1000 m wide, which is what they are for a paddler (no confinement). `xs_flat_frac` flags them.

Derived per reach in `xsec.derive` (from the joined table's flood and gradient, so it follows the MAF correction):

- `xs_omega10_wm2`: unit stream power of the mean annual flood spread over the 10 m-stage width,
  ω10 = ρ g Q S / w10 (W/m²).
- Manning flood hydraulics (`xsec.hydraulics`): the stage at which the median section carries the MAF,
  Q = A R^(2/3) S^(1/2) / n with R = A / P, n = 0.045, S = smoothed gradient (floored at 0.1 m/km); log-log
  interpolation between the stages. From it: mean velocity V = Q / A and unit stream power ρ g Q S / w at that
  stage.

### The ML features

| feature | definition |
|---|---|
| `xs_w10`, `xs_w20` | log10 median section width at the 10 m and 20 m stages (m, 8-1000) |
| `xs_wall` | lower valley wall: the lower side's highest ground within 150 m, m (median of sections) |
| `xs_h2a` | cross-sectional height-to-area: lower wall height / sqrt(flow area at the 20 m stage), log10 |
| `xs_pinch` | constriction index: narrowest section's width / the reach's median width at the 20 m stage, log10 (0 = no pinch) |
| `xs_omega10` | log10 ω10, unit stream power at the MAF over the 10 m-stage width |
| `xs_vel`, `xs_omega_q` | log10 Manning velocity and unit stream power at the MAF (registry only, not in `gbm_xsec`) |

A reach with no valid section (no DEM near the line) counts as wide open (1000 m, no wall, no pinch).

## Resolution: what the 8 m DEM can resolve

The national DEM is the LINZ 8 m DEM interpolated from 20 m contours. It has no channel below the water surface,
and between the stream line and the next contour its floor is interpolated, so a 1-2 m stage is mostly
interpolation. The three reference areas also hold a 5 m DEM resampled from 1 m LiDAR (100 % LiDAR there), so the
same reaches (the 5 m network) can be measured on both. Spearman rank correlation between the 8 m and the 5 m value
per reach (reaches of 2 km² or more; Kaimai n = 4711, Karangahake 1929, Tutamoe 5393), and the median ratio 8 m /
5 m:

| measure | Kaimai | Karangahake | Tutamoe | median ratio 8 m / 5 m |
|---|---:|---:|---:|---:|
| width at 1 m | 0.51 | 0.51 | 0.29 | 5-9 |
| width at 2 m | 0.59 | 0.58 | 0.35 | 4-5 |
| width at 5 m | 0.72 | 0.79 | 0.68 | 2.1-2.4 |
| width at 10 m | 0.78 | 0.90 | 0.86 | 1.5-1.9 |
| width at 20 m | 0.87 | 0.94 | 0.91 | 1.2-1.4 |
| area at 10 m | 0.76 | 0.88 | 0.81 | 1.9-2.2 |
| bank within 50 m (lower side) | 0.77 | 0.68 | 0.57 | 0.04-0.18 |
| wall within 150 m (lower side) | 0.90 | 0.86 | 0.82 | 0.25-0.48 |
| shape A(10) / (10 w(10)) | 0.15 | 0.30 | 0.32 | |
| Manning flood stage | 0.47 | 0.34 | 0.41 | 0.3-0.5 |

The 8 m DEM is wider and flatter at every stage (widths 1.5-2x at 10 m), but it ranks reaches nearly as LiDAR does
from the 10 m stage up. The bank within 50 m is almost flat on it, and the V-versus-U shape is not reproduced at all.

The ML features themselves, raw and *partial* (residuals after a linear fit on log MAF and log gradient, which both
DEMs share; this is what the feature can add):

| feature | raw (Kaimai / Karangahake / Tutamoe) | partial |
|---|---|---|
| `xs_w10` | 0.78 / 0.90 / 0.86 | 0.62 / 0.79 / 0.77 |
| `xs_w20` | 0.87 / 0.94 / 0.91 | 0.77 / 0.89 / 0.86 |
| `xs_wall` | 0.90 / 0.87 / 0.82 | 0.88 / 0.90 / 0.83 |
| `xs_h2a` | 0.89 / 0.90 / 0.85 | 0.80 / 0.82 / 0.77 |
| `xs_pinch` | 0.42 / 0.70 / 0.53 | 0.36 / 0.50 / 0.47 |
| `xs_omega10` | 0.95 / 0.97 / 0.97 | 0.69 / 0.83 / 0.79 |
| `xs_vel` (Manning) | 0.79 / 0.83 / 0.77 | **0.17 / 0.14 / 0.14** |
| `xs_omega_q` (Manning) | 0.91 / 0.95 / 0.92 | **0.21 / 0.20 / 0.16** |

The Manning velocity and stream power look consistent only because both DEMs share Q and S. What the section adds to
them is noise on the 8 m DEM: the floods sit far below the stages it resolves. The median Manning stage of a reach of
2 km² or more is 0.3-0.4 m on the 8 m DEM (often at the 0.25 m floor; 0.6-1.2 m for 50 km² and more) against
0.8-1.4 m on LiDAR (2-3.5 m for 50 km² and more), and the median velocity is about half the LiDAR one (0.5-0.8 against
1.2-1.8 m/s). They stay in the registry for a LiDAR-only study, not in `gbm_xsec`. The constriction index at the
10 m stage was worse (partial 0.24-0.35); at the 20 m stage it is usable but weak. A neighbourhood version (narrowest
section over the median of the reach and its neighbours) was no better than the reach's own median.

**Decision**: the national features use the 8 m DEM only, at the 10 and 20 m stages and the 150 m walls, so every
reach in NZ is measured the same way. The 5 m extraction (`river-finder xsec <region> --dem 5m`) is for this
comparison only; a reference area is scored from its 8 m sections (`data/<region>/xsec_8m.pkl`).

## Cost

Measured on 14 national tiles (155 260 reaches of 1 km² or more, 744 726 sections) on this 4-core, 15 GB machine:
the 8 m DEM of a tile (its core plus 1 km, 8 250² cells, ~135 MB) downloads from the public `nz-elevation` bucket in
about 35 s; reading it takes 2 s and the sections 1-3 s. The next tile's DEM downloads while the current one is
measured, so a tile takes about 33 s of wall clock (14 tiles in 7.8 min), with a peak of 1.7 GB. The 114 national
tiles with reaches should take about **65 minutes and under 2 GB** (network-bound). Each tile's DEM is deleted after
use (`--keep-dem` keeps it); the outputs are ~1.5 MB per tile and ~170 MB merged. A reference area takes a few
seconds per DEM.

## Results on a sample (14 tiles)

Sample: 14 national tiles chosen for many guide rivers plus background, covering 11 guide regions (x02y04, x02y05,
x03y05, x04y06, x05y07, x06y08, x07y09, x07y10, x07y11, x11y11, x11y14, x12y16 Kaimai, x12y17 Karangahake, x09y20
Tutamoe): 111 524 domain reaches (2 km² or more) with sections, 6 020 positive reaches on 115 guide rivers, 105 504
background. All numbers below are on these reaches only (the cached `ml_table` with the MAF correction on).

### Separation and overlap

Each feature alone (AUC of positives against background; within style; within style × flood quintile; < 0.5 means
lower values are the runs), and its Spearman correlation with existing features over the domain:

| feature | AUC | style | style × size | ρ gorge | ρ h_to_w | ρ valley_w | ρ confine | ρ MAF | ρ gradient |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `xs_w10` | 0.460 | 0.431 | 0.381 | -0.91 | -0.88 | 0.60 | 0.48 | 0.03 | -0.67 |
| `xs_w20` | 0.440 | 0.414 | 0.380 | -0.91 | -0.87 | 0.57 | 0.48 | 0.00 | -0.64 |
| `xs_wall` | 0.554 | 0.577 | 0.613 | 0.91 | 0.85 | -0.54 | -0.45 | -0.02 | 0.68 |
| `xs_h2a` | 0.552 | 0.578 | 0.617 | 0.92 | 0.87 | -0.56 | -0.46 | -0.02 | 0.68 |
| `xs_pinch` | 0.494 | 0.466 | 0.424 | -0.52 | -0.51 | 0.30 | 0.21 | 0.07 | -0.42 |
| `xs_omega10` | 0.643 | 0.777 | 0.618 | 0.79 | 0.79 | -0.57 | -0.70 | 0.33 | 0.80 |
| `xs_vel` | 0.587 | 0.717 | 0.606 | 0.80 | 0.82 | -0.60 | -0.65 | 0.23 | 0.84 |
| `xs_omega_q` | 0.546 | 0.665 | 0.586 | 0.80 | 0.84 | -0.64 | -0.64 | 0.15 | 0.90 |
| *for reference:* gorge | 0.548 | 0.572 | 0.592 | | | | | | |
| h_to_w | 0.493 | 0.514 | 0.577 | | | | | | |
| valley_w | 0.574 | 0.576 | 0.478 | | | | | | |
| confine | 0.271 | 0.230 | 0.396 | | | | | | |

The section widths and walls are mostly another measure of gorge depth (|ρ| about 0.9) rather than of the HAND
valley width (ρ 0.6): narrow at 10-20 m above the bed means steep walls next to the river. Within style and size they
separate slightly better than gorge depth and height-to-width (0.61-0.62 against 0.58-0.59 for the walls and narrow
widths). A linear fit on the `gbm_terrain` numeric inputs explains 77-81 % of the variance of the widths, walls and
height-to-area, 96 % of ω10, but only 10 % of the constriction index; what is left still separates a little
(residual AUC within style 0.60 for `xs_h2a` and for narrow pinch points, 0.58 for narrow widths).

### Grouped CV with and without (same folds)

`ml.run_cv` on the sample (5-fold outlet-catchment folds, 10 repeats; every model sees the same folds and the same
background sample, so the differences are paired). `gbm_xsec_w` is `gbm_terrain` + the two widths only (not in the
zoo).

| model | AUC | style-matched | style × size | recall top 10 % | recall top 5 % |
|---|---:|---:|---:|---:|---:|
| `gbm_terrain` | 0.8956 ± 0.0057 | 0.8787 ± 0.0061 | 0.7199 ± 0.0133 | 0.669 | 0.471 |
| `gbm_xsec` | 0.9000 ± 0.0054 | 0.8839 ± 0.0058 | 0.7323 ± 0.0126 | 0.686 | 0.488 |
| paired gain | **+0.0044 ± 0.0017** (10/10) | **+0.0053 ± 0.0019** (10/10) | **+0.0124 ± 0.0039** (10/10) | +0.017 (9/10) | +0.018 (10/10) |
| `gbm_xsec_w` gain | +0.0039 ± 0.0018 | +0.0048 ± 0.0021 | +0.0126 ± 0.0049 | +0.016 | +0.016 |
| `logit_plus` | 0.9092 ± 0.0013 | 0.8913 ± 0.0016 | 0.7363 ± 0.0052 | 0.724 | 0.501 |
| `logit_xsec` gain | +0.0075 ± 0.0006 (10/10) | +0.0096 ± 0.0008 (10/10) | +0.0307 ± 0.0025 (10/10) | +0.025 | +0.063 |

(± = sd over the 10 repeats; (k/10) = repeats in which the new model is better.) In a 5-repeat run, ω10 alone added
nothing (-0.001), the wall, height-to-area and pinch features without the widths gave half the gain (+0.0034
style-matched), and adding the Manning velocity and stream power to `gbm_xsec` gave nothing on top (-0.0005).
LightGBM's gain: `xs_w10` 0.036 (6th of 26 inputs, after MAF, Melton, plateau, hard rock and head), `xs_omega10`
0.020, `xs_w20` 0.017, `xs_h2a` 0.016, `xs_wall` 0.012, `xs_pinch` 0.006; together 0.11.

Leave one region out (11 regions; train on the other 10 regions' catchments in the sample): mean style-matched AUC
0.8426 -> 0.8515 (`gbm_xsec`), style × size 0.6721 -> 0.6894; better in 9 of 11 regions (largest in Canterbury
+0.033, Waikato +0.018, Bay of Plenty +0.017; West Coast -0.003, Whanganui -0.001).

Caveats: the sample has 43 % of the national positives, and with that little data `logit_plus` beats `gbm_terrain`
here (0.891 vs 0.879 style-matched), the reverse of the national CV (0.931 vs 0.936). The national gain for LightGBM
may well be smaller than on the sample. The background holds unknown runs, so every AUC is a lower bound.

## Verdict

The cross-sections add a small, consistent amount of information beyond the existing terrain features: +0.005
style-matched AUC and +0.012 style × size for LightGBM in every repeat, and about twice that for the logistic model.
The information is local confinement (how narrow the valley is 10-20 m above the river and how high its lower wall
is), which the HAND valley width and the 150 m gorge depth only partly capture. The hydraulic reading the issue hoped
for (velocity and unit stream power at the flood) is not measurable on the 8 m contour DEM: the flood sits below what
it resolves, and the Manning numbers agree with LiDAR only through Q and S. A LiDAR-only version (where 1 m LiDAR
exists) could revisit that, but it would not be comparable across NZ.

## Limits

- 8 m contour DEM: widths are 1.5-2x the LiDAR widths at 10 m, banks within 50 m nearly flat, V-versus-U shape not
  reproduced; only the rank order at 10-20 m stages is trustworthy.
- The channel bottom is the DEM's, not the bed: there is no channel below the water surface in either DEM.
- Censored sections (no rise within 500 m) cap widths at 1000 m, so open valleys, plains and lakes tie at the top.
- Sections near confluences cross the tributary's valley floor and come out wider; the median over a reach's
  sections damps it.
- The constriction index compares a reach's ~5 sections with each other; on the 8 m DEM it agrees with LiDAR only
  moderately (partial ρ 0.36-0.50).

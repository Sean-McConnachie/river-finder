# Audit: outlier specific MAF values (rf-xyk.6, 2026-11-22)

Specific MAF is the mean annual flood per unit area (m³/s/km²). `reaches._attach_flood` takes it from
NIWA's Henderson & Collins (2018) flood statistics on REC1 (H&C MAF / REC1 `Areakm2`) at the nearest REC1
reach within 400 m whose area is 0.4–2.5 times ours. It then sets `maf_m3s` = our area × that value. The
issue asked whether values such as the Waiokumurau's 4.8 m³/s/km² are wrong, and whether a sanity cap or
spatial smoothing is needed.

**Short answer.** The Waiokumurau value is H&C's own estimate. It is high for Northland, but it is not an
artefact. The worst errors come from our join, not from H&C:

1. **Reaches with no REC1 match get the median specific MAF of their whole tile.** This hits 11 % of
   reaches of 2 km² or more and 30 % of rivers over 1,000 km². The worst case is the lower Buller at
   Westport: 45,240 m³/s, against 4,868 m³/s gauged at Te Kuha.
2. **H&C goes above anything gauged in NZ on small, steep, wet catchments:** Mt Taranaki at
   45 m³/s/km², the Tararua at 24, the Raukumara at 21 and South Westland at 25.

The fix in `src/river_finder/maf.py` refills unmatched reaches from their network neighbours and caps
specific MAF at the national gauge envelope. It also adds a `maf_flag` column. It is **off by default**
(`config/maf.toml`, `--maf-correction on|off`), because turning it on changes every score.

**Effect when on:**

- 66,979 reaches change MAF (11 % of the domain); 919 of them are capped.
- 63 of the 66 rivers in v3's top-100 unlisted runs stay. The two biggest movers among them are the
  unnamed South Westland valley floors, whose MAF falls from about 9,000 to 3,500 m³/s.
- At 528 gauges, our MAF is within 1.5× of the gauged value at 75.9 % of gauges, up from 74.8 %.
- In paired CV, the logistic models gain +0.003 to +0.005 style-matched AUC. The LightGBM recipe of v3
  is unchanged within noise.

Recommendation: turn refill and cap on at the next national rebuild (see the end of this document).

Analysis outputs, not committed, are in `data/analysis/maf/`: `reaches.pkl`, `rec1_match.pkl`,
`gauge_check.csv`, `impact.json`, the `v3_runs_*.csv` files and `cv_*.json`.

## Data and method

- **Reaches.** I used the national 8 m table (`data/nz/segments_table.pkl` and `ml_table.pkl`, 1,104,794
  reaches), with `specific_maf` and `specific_maf_matched` joined by `gid` from the tiles'
  `segments_core.gpkg`. The **domain** is the 594,736 reaches of 2 km² or more.
- **REC1 behind each value.** I re-ran the join with the same rule as `_attach_flood`. It found the same REC1
  reach for 100 % of matched reaches, checked by comparing the specific MAF to 4 decimals. I also took the
  REC2 `cum_area_km2` of the nearest REC2 reach of similar size, to test for area mismatches.
- **Gauges.** I used the 634 flow gauges behind H&C 2018 (NIWA's `FlowGauges` layer, cached in
  `data/nz/gauges.json`). For each gauge I used the catchment area and the gauged 2.33-year flood
  (`Data_2_33y`), which is the at-site mean annual flood. 593 gauges drain 1 km² or more. Each gauge was
  matched to our reach (528 gauges) and to an H&C REC1 reach (588 gauges): the nearest reach within 800 m
  whose area is within ±50 % of the gauge's.
- **H&C uncertainty.** The live REC1 layer also has `HCse_MAF`. On a 582-reach sample (every 997th
  feature), `HCse_MAF / H_C18_MAF` is 0.4998 on **every** reach. So H&C quote a flat ±50 % standard error. It
  says nothing about which reaches are worse than others.

### What the literature and the gauges allow

- **Henderson & Collins, *Regional Flood Estimation Tool for New Zealand*.** Part 1 is NIWA Client Report
  2016049CH (August 2016). Part 2 is NIWA Client Report 2018177CH (June 2018). Part 2 says its dataset has
  twice the sites and three times the annual maxima of the earlier national study. I could not read the
  reports from this session, because niwa.co.nz is outside the session's allowed network. So the method
  details below come from the published layer and our own checks, not from the report text.
- **McKerchar & Pearson (1989), *Flood frequency in New Zealand*, Hydrology Centre Publication 20.** This
  is the earlier national study. It mapped MAF / A^0.8, so specific MAF ∝ A^−0.2. (I quote this from
  memory; I did not re-check it this session.) Our fit to the H&C gauges gives a 90 % quantile slope of
  −0.25 and a median slope of −0.20 for log specific MAF on log area, which agrees.
- **The gauged record is the hard bound.** The issue suggests "up to ~2–3 m³/s/km² for small
  catchments". That is too low for the West Coast, where small catchments are gauged at 7–14. The highest
  gauged values for catchments of 1 km² or more are:
  - Cropp at Gorge: 13.65 (12.2 km²);
  - Ivory at Ripplerock: 10.58 (2.3 km²);
  - Opanuku at Vintage Reserve: 7.84 (6.3 km²);
  - Waimangaroa at Smokestack: 7.37 (19.6 km²);
  - Makawhio at Rocks: 6.51 (135 km²);
  - Whataroa at SH bridge: 5.93 (445 km²);
  - Haast at Roaring Billy: 3.67 (1,020 km²);
  - Grey at Dobson: 0.99 (3,830 km²), the highest of the 19 gauges over 3,000 km².

  The envelope through these is **E(A) = 27.2 · A^−0.25** m³/s/km². With b = 0.25, Whataroa sets c, and
  Cropp and Hokitika at Gorge come next. That gives 22.9 at 2 km², 15.3 at 10 km², 8.6 at 100 km² and
  4.8 at 1,000 km².
- **H&C is unbiased at the gauges, even the extreme ones.** At the 588 gauges matched to a REC1 reach,
  its ratio to the gauged MAF has a median of 1.00, and 76 % of gauges are within 1.5×. Examples: Cropp 13.55 against 13.65 gauged, Whataroa 5.43
  against 5.93, Waiohine at Gorge 5.38 against 4.91. The values in doubt are its extrapolations upstream of
  the gauges.

## Findings

### Distribution against area

Specific MAF, domain reaches (raw is as used today; the gauge columns use the H&C gauges in that band):

| area km² | reaches | unmatched | raw median | raw p99 | raw max | gauges | gauged median | gauged max | envelope at band start |
|---|---|---|---|---|---|---|---|---|---|
| 2–10 | 318,646 | 9 % | 1.10 | 13.8 | 45.0 | 50 | 1.12 | 10.6 | 22.9 |
| 10–100 | 190,953 | 11 % | 0.88 | 10.4 | 20.6 | 203 | 1.03 | 13.7 | 15.3 |
| 100–1,000 | 66,167 | 18 % | 0.72 | 8.2 | 11.4 | 265 | 0.62 | 6.5 | 8.6 |
| 1,000–30,000 | 18,961 | 30 % | 0.45 | 5.6 | 10.0 | 67 | 0.39 | 3.7 | 4.8 |

The medians follow the gauges. The tails do not:

- In small catchments, the raw maximum is 2–4× the highest gauged value.
- In large rivers, the 99th percentile (5.6) is 1.5× the highest gauged value over 1,000 km². These are
  almost all tile-median fills.

By region, matched reaches only (H&C's own values):

| region | median | p99 | max | reaches > 10 |
|---|---|---|---|---|
| West Coast | 5.77 | 17.9 | 25.0 | 6,557 |
| Wellington (Tararua) | 1.68 | 17.8 | 24.6 | 713 |
| Gisborne (Raukumara) | 2.49 | 16.6 | 21.2 | 1,055 |
| Southland (Fiordland) | 0.97 | 10.8 | 16.7 | 745 |
| Tasman | 1.82 | 10.4 | 13.9 | 282 |
| Taranaki | 1.34 | 9.8 | **45.0** | 175 |
| Canterbury | 0.75 | 7.9 | 20.8 | 471 |
| Northland | 1.68 | 6.2 | 7.2 | 0 |
| other 8 regions | 0.4–1.5 | 5.0–8.9 | 7.5–23.5 | 0–175 |

### Outlier classes

**1. Unmatched reaches: the tile-median fill (the biggest error).** `_attach_flood` gives every reach
without a REC1 match the median specific MAF of the matched reaches in its tile. That median comes from
small streams, so it is far too high for big rivers.

- 67,106 domain reaches are unmatched (11.3 %). That is 9 % of reaches at 2–10 km² but 30 % of reaches
  over 1,000 km².
- The REC1 line of a big river is often more than 400 m from our DEM channel: braided beds, lakes and
  river mouths. Within 400 m, only small tributaries pass the area-ratio test. Canterbury alone has 22,408
  unmatched domain reaches.
- On unmatched reaches over 300 km², the fill is a median 2–3× the specific MAF of matched reaches of the
  same region and size, and up to 10× at the 90th percentile.

The worst cases:

| reach | area km² | raw MAF m³/s | refilled | check |
|---|---|---|---|---|
| lower Buller, Westport (tile fill 7.04) | 6,428 | 45,240 | 4,296 | 4,868 gauged at Te Kuha (6,350 km²) |
| Waiau, Southland | 5,961 | 19,140 | 4,163 | — (regulated river) |
| lower Clutha | 20,904 | 17,013 | 2,727 | — |
| lower Waitaki | 11,797 | 13,757 | 1,676 | — |
| lower Waikato | 14,486 | 12,014 | 1,896 | — |
| Manawatu at Teachers College | 3,912 | 3,481 | 1,987 | 1,591 gauged (H&C 1,979) |
| Whangaehu at SH3 | 1,949 | 1,337 | 586 | 522 gauged (H&C 553) |
| Waimakariri at Old Highway Bridge | 2,760 | 2,093 | 1,159 | 1,494 gauged (H&C 1,170) |
| Moawhango at Waiouru | 281 | 253 | 99 | 101 gauged |
| Omarama above Tara Hills | 184 | 64 | 22 | 18.8 gauged (H&C 22.8) |

The fill also drives most downstream drops. The raw table has 11,754 domain reaches whose MAF falls by more
than 10 % to the reach below. The refill removes 5,275 of them.

**2. H&C above the national gauge envelope (implausible, but rare).** 902 matched domain reaches (0.15 %)
are above E(A). They sit on Mt Taranaki, the Tararua, the Raukumara and the Southern Alps.

- **Mt Taranaki ring plain.** On the upper cone, Minarapa, Peters, Pyramid and Kokowai Streams have
  36–45 m³/s/km² at 2–3.5 km². For example, REC1 reach 6006407 (Minarapa) has 2.29 km² and an H&C MAF of
  103 m³/s, so our 2.2 km² reach gets 97–105 m³/s. REC1 and REC2 areas agree (2.29 and 2.49 km²), so this
  is the flood estimate itself, not the area. The ring-plain gauges measure 5.3–5.7 (Manganui, Maketawa
  and Waiwhakaiho at SH3), so the upper-cone values are 8× the gauges.
- **Tararua.** The Waiohine, Park, Ruamahanga, Otaki and Eastern Waiotauru headwaters reach 22–24 at
  2–3 km². Waiohine at Gorge (180 km²) is gauged at 4.91.
- **Raukumara.** Kopuapounamu, Mangaoparo, Taurangakautuku and Raparapaririki reach 18–21 at 2–35 km².
  The highest of the 21 Gisborne gauges is 3.35.
- **South Westland.** Unnamed headwaters reach up to 25. Ivory at Ripplerock: H&C gives 17.3 where the
  gauge measures 10.6.

**3. High but plausible: the Waiokumurau.** The Waiokumurau Stream is a tributary of the upper
Mangakahia in the Tutamoe reference area. It is at −35.61 S, 173.73 E at 51 km² and runs up to −35.67,
173.69.

- Its specific MAF is 4.83 at 51 km² (MAF 246 m³/s) and rises to 6.8 at 0.6 km².
- Our area agrees with REC1 reach 1015445 (51.0 against 51.5 km²). H&C's MAF for that reach is 249 m³/s.
  So this is neither an area mismatch nor a join artefact.
- It is the top of the Northland range: the regional p99 is 6.2. The highest of 29 Northland gauges is
  Ngunguru at Dugmores Rock, 4.45 at 12.5 km². Mangakahia at Gorge (246 km²) is gauged at 2.10, and H&C
  gives 1.55 there. Scaled to 51 km² with A^−0.25, the Mangakahia gauge implies about 3.1, so H&C is
  about 1.5× above it.
- That is well inside H&C's ±50 % standard error, and below the envelope (10.2 at 51 km²). **No change.**

The Waiapu value flagged in the v3 audit is the same kind of case: 8.15 m³/s/km² at 79 km², MAF
646 m³/s. It is H&C's own number, and REC1 and REC2 areas agree. It is 2.4× the highest Gisborne gauge
(3.35), but it is below the envelope (9.1), so it stays. The Karangahake reference area has similar
values: Maratoto, Stony, Grace Darling and McBrinn Streams have 14–18 at 1–5 km². Waiorongomai at Old
Quarry (7.9 km²) is gauged at 4.72.

**4. Very large specific MAF in REC1 itself (mostly does not reach us).** Some REC1 reaches have tiny
areas with large MAFs, for example:

- Whanganui 7007641: 0.29 km², 55.7 m³/s, which is 190 m³/s/km²;
- Moawhango 7017052: 0.68 km², 116.9 m³/s;
- several Halswell River and Taranaki reaches.

These look like REC1 area artefacts on diverted or modified reaches. The 0.4–2.5 area-ratio rule keeps
them off our reaches of 2 km² or more. The highest specific MAF in our domain is 45 (Taranaki).

### Area mismatch is not the cause

- For matched domain reaches, the ratio of our area to REC1's has a median of 0.986 (p5 0.70, p95 1.41).
  REC2 to REC1 has a median of 1.003 (p5 0.92, p95 1.11).
- Among reaches above 14 m³/s/km², our area / REC1 is 0.97 (p10–p90 0.81–1.10) and REC2 / REC1 is 1.01.
- Using REC's area instead of ours would not change specific MAF, which comes from REC1 alone. It would
  move `maf_m3s` by our area / REC1, a median of 1.4 %. So it is not worth doing.

### How MAF reaches the scores

- Two model features use it: `maf` (log10 MAF) and `power` (ρg · MAF · gradient). Every logistic and
  LightGBM recipe has both.
- The reach style uses it. Bankfull width is 4 · MAF^0.5, which sets the confinement ratio and so the
  Montgomery & Buffington style. The style sets the `auc_style` strata.
- The run finder uses it for the flow band (2–8–300–2000 m³/s) and the flow-scaled gradient.
- The `auc_size` strata are bins of log10 MAF.

## Impact on rankings and known runs

These numbers compare v3 (`models/v3.json`, gbm_terrain) on the national ML table, raw against corrected.
The corrected table has refill and cap on, with styles recomputed. Percentiles are among unlisted domain
reaches, as in the v3 audit.

**What the correction changes.**

- 66,979 domain reaches change MAF: 66,687 refilled and 919 capped. The new / raw ratio is a median of
  0.61 (p5 0.20, p95 1.49).
- 1,345 reaches change style. Most move from bedrock_slides_and_gorges or continuous_read_and_run to a
  less confined style: a smaller flood gives a narrower bankfull estimate.

**Top of the unlisted ranking.**

- 571 of the 5,806 reaches in the top 1 % had a flagged MAF.
- After the correction, 5,595 (96 %) are still in the top 1 %.
- Of the refilled reaches in the top 1 %, 515 of 758 stay. Of the capped ones, 168 of 185 stay.

So inflated MAFs did lift some reaches into the top 1 %, but not many.

**Top-100 runs (the audit list).**

- 63 of the 66 rivers are still in the top 100.
- Route Burn (89 → 159) and Okuru (96 → 102) drop out. Raparapaririki Stream (103 → 90) comes in.
- 10 of the top 100 runs had a flagged reach:
  - Whataroa: rank 3 stays 3. Its MAF at the bottom of the run falls from 4,290 to 2,519 m³/s; the
    Whataroa at SH bridge (445 km²) is gauged at 2,640.
  - Aorere: 9 → 8; MAF 2,324 → 1,313 m³/s.
  - Two unnamed South Westland valley-floor runs: 69 → 42 and 94 → 55. Their MAF falls from about
    9,000 to 3,500 m³/s. LightGBM's response to MAF is not monotonic, so a smaller, more typical flood
    can rank higher.
- The 12 "plausible new leads" in the v3 audit are unchanged or move by a few places. For example,
  Mueller goes 45 → 57, its MAF 579 → 620 m³/s.

**Known runs.**

- 48 of the 195 known rivers with positive reaches have at least one flagged reach.
- v3's in-sample percentiles for them barely move: most rivers change by less than 0.5 points.
- The exception is the Waikato, whose 16 positive reaches are all refilled: 99.8 → 95.1.
- The out-of-fold effect is in the CV section below.

**Reference areas (5 m).**

| area | reaches ≥ 1 km² | refilled | capped | drops flagged | biggest refill |
|---|---|---|---|---|---|
| Kaimai | 6,730 | 710 | 0 | 6 | an unnamed 572 km² reach, 415 → 269 m³/s |
| Karangahake | 2,917 | 241 | 0 | 45 | an unnamed 70 km² reach, 157 → 128 m³/s |
| Tutamoe | 7,861 | 470 | 0 | 189 | an unnamed 668 km² reach of the upper Hokianga Harbour, 2,173 → 606 m³/s |

The Tutamoe case is the tile fill at its plainest:

- The Waima River at 518 km² is matched to REC1, with H&C giving 0.97 m³/s/km² (501 m³/s).
- Below its outlet (173.58 E), 106 harbour reaches of 400–668 km² have no REC1 match. All of them got the
  tile median of 3.25, so the 668 km² reach at 173.45 E, −35.41 carried 2,173 m³/s: 4.3× the flood of
  the river that feeds it.
- The refill carries the Waima's own value down: 0.97 × (668/518)^−0.25 gives 606 m³/s.
- There is no gauge on the Waima. The nearest Northland gauges of similar size (Waipapa at Forest
  Ranger, 122 km²; Mangakahia at Gorge, 246 km²) give 2.1–2.4 m³/s/km².

**At the gauges.** Our MAF against the gauged MAF at 528 gauges:

| | mean abs. log10 error | within 1.5× | over 2× | under ½ |
|---|---|---|---|---|
| raw | 0.130 | 74.8 % | 28 | 23 |
| corrected | 0.126 | 75.9 % | 24 | 23 |
| H&C on REC1 (the 519 of these with a REC1 match) | 0.124 | 78.4 % | 23 | 22 |

At the 11 refilled gauge reaches, the refill moves our MAF closer to the gauged value in 8 cases:

- Manawatu at Teachers College: 3,481 → 1,987 (1,591 gauged);
- Whangaehu at SH3: 1,337 → 586 (522);
- Moawhango at Waiouru: 253 → 99 (101);
- Omarama above Tara Hills: 64 → 22 (18.8);
- Whakatane, Kumeti, Waimakariri and Glenbervie.

It moves away in 3:

- Hamilton Burn, at two gauges: 113 → 98 m³/s against 122 and 137 gauged;
- Whanganui at Headwaters: 76 → 91 against 45 gauged. That reach is below the Tongariro Power Scheme
  intakes, so natural flood statistics overstate it either way.

## The correction (implemented, on since 2026-11-24)

`src/river_finder/maf.py`, configured in `config/maf.toml` `[correction]`:

1. **`cap`** limits specific MAF to `envelope_margin · envelope_c · A^−envelope_b`, which defaults to
   27.2 · A^−0.25. `fit_envelope` reproduces `c` from gauges. Matched reaches are capped first, so a
   refill never borrows an implausible value.
2. **`refill`** applies to unmatched reaches, and to matched reaches with zero MAF. Each takes the
   specific MAF of the nearest matched reach up the main stem and the nearest downstream, each scaled by
   (A / A_ref)^−0.25. The two are combined as a geometric mean weighted by 1 / (|ln A/A_ref| + 0.05). A
   reach with neither neighbour keeps its value.

   I tested this by hiding 169,209 matched reaches, in runs of 3–40 reaches down the network, and
   predicting them. The network refill has a median absolute log error of 0.021 and is within 1.5× of the
   REC1 value 98 % of the time. The tile-median fill has an error of 0.50 and is within 1.5× only 41 % of
   the time. Over 1,000 km², the errors are 0.007 and 1.16.
3. Capped refills are capped again.
4. **`drop`** flags a reach when the MAF of the reach below is more than `drop_tol` (10 %) lower. It is a
   flag only.
5. **`monotone`** (default off) lowers every reach to the MAF of the reach below it. It stays off because
   lakes and hydro schemes really do lower floods downstream. Turned on, it would clamp, for example, the
   whole Lake Taupō catchment to the controlled outlet.

`maf_flag` records what was done: `""`, `refill`, `cap`, `refill+cap`, `drop`, `monotone` (joined with
`+`). `maf_raw_m3s` keeps the uncorrected value, so applying again or reverting (`maf.revert`) always
starts from the raw value. `power_kw_m` is recomputed.

**Where it runs when on:**

- `scoring.prepare`, which covers reference and national scoring, `segments_scored.gpkg`, the viewer
  and the run finder;
- `ml.ml_table` / `ml.sync_maf`, on the whole-NZ network. This step also recomputes reach styles. It
  brings a cached table in line with the switch, so `ml cv` and `ml audit` follow `--maf-correction`
  without a table rebuild.

`models.national_table` keeps `specific_maf_matched`, `maf_raw_m3s` and `maf_flag` when the scored
segments have them. For a table cached before this change, `maf.matched_from_tiles` reads the matched
flag from the tiles.

**Considered, not adopted:**

- **A regional or local gauge envelope.** I tried the gauges within 50 km, with a 2× margin. It would cap
  2,225 more reaches: Gisborne 660, Canterbury 464, Wanganui 402, Hawke's Bay 358. But most of those are
  on ungauged wet ranges, such as the Ruahine (Oroua, Kawhatau, Makaroro, Pohangina) and the Main Divide,
  where the gauges sit on the drier plains. A local envelope there punishes real rainfall gradients. It
  does catch the Waiapu headwaters (47 reaches). It misses the Karangahake streams, because Waiorongomai
  (4.72 at 7.9 km²) lifts the local envelope above them. It would be useful as an audit flag, not a cap.
- **A median filter over neighbours.** H&C values are piecewise constant per REC1 reach, so a median
  filter only blurs confluences. The outlier clusters, such as the Taranaki cone, are spatially coherent,
  so a local median keeps them.
- **REC's own area.** It makes no difference (see above).

## Effect on model CV

**How it was run.** Both tables use the same folds, seeds and labels: 14,185 positive reaches and
580,550 background reaches. Both are scored on the **raw** table's style and size strata, so the
yardstick is the same. Scoring each on its own strata gives the same numbers, except `auc_size`, whose
bins move. I used the catchment-grouped 5-fold CV of `ml.run_cv` and the LORO of `ml.run_loro`. The
logistic models ran 3 repeats. gbm_terrain ran 5 repeats, and its LORO was run with 3 seeds. Raw numbers
match `models/cv/results.json` to within 0.001–0.004. Results are in `data/analysis/maf/cv_*.json`.

| model | metric | raw | corrected | paired change (sd over repeats) |
|---|---|---|---|---|
| grad_flow (gradient + log MAF) | auc_style | 0.8959 | 0.9006 | **+0.0047** (0.0001) |
| | auc_size | 0.5496 | 0.5855 | **+0.036** (0.0003) |
| | recall_top10 | 0.727 | 0.739 | +0.012 (0.0007) |
| | LORO auc_style | 0.8851 | 0.8897 | +0.0046 |
| logit13 (v2 recipe) | auc_style | 0.9153 | 0.9186 | **+0.0033** (0.0001) |
| | auc_size | 0.7148 | 0.7298 | **+0.015** (0.0003) |
| | recall_top10 | 0.786 | 0.798 | +0.0115 (0.0003) |
| | LORO auc_style | 0.8991 | 0.9023 | +0.0032 |
| gbm_terrain (v3 recipe) | auc_style | 0.9292 | 0.9288 | −0.0004 (0.0004) |
| | auc_size | 0.7882 | 0.7876 | −0.0005 (0.0019) |
| | recall_top10 / top5 | 0.811 / 0.658 | 0.813 / 0.662 | +0.0015 (0.0023) / +0.0037 (0.0038) |
| | LORO auc_style (3 seeds) | 0.9114 | 0.9102 | −0.0011 (0.0012) |

**The logistic models gain on every metric.** They can only use MAF monotonically, through log MAF and
stream power, so a fill that is 3× too big is pure noise to them. The size-stratified AUC moves most
(+0.015 to +0.036). Within flood-size bins, positives rank better once big rivers stop sitting in the
wrong bin.

**LightGBM is unchanged within noise.** The AUCs move −0.0004 and top-5 % recall +0.004, both within one
sd. Trees can partly learn around a biased input; for example, the fill is constant within a tile while
area grows downstream.

**Known runs, out-of-fold.** I took each river's median style-matched percentile from gbm_terrain, over 5
repeats.

- For the 48 rivers with a flagged reach, the median change is −0.01 points. The range is −3.8 to +2.2:
  - down: Cameron (Canterbury) −3.8, Greenstone −3.1, Waingaro −1.9, Waipara (West Coast) −1.6;
  - up: Wairua +2.2, Waiatoto +1.1, Okarito +1.0.
- Unflagged rivers move by −0.9 to +1.4 (p5–p95) from refitting alone.

So for the production recipe, the case for the correction is **physical plausibility**: correct floods
for big rivers in the viewer, the run finder's flow band, stream power and reach style. It is not CV. CV
does not get worse, and the simpler models get better. Per-river results are in
`data/analysis/maf/known_rivers_oof_gbm_terrain.csv`.

## Recommendation

1. **Turn on `refill` and `cap`** (`apply = true`, or `--maf-correction on`) at the next national
   rebuild. The refill fixes a join bug with errors up to 10× on big rivers. The cap trims 919
   physically implausible reaches. Both are small, documented changes, recorded per reach in `maf_flag`.
2. **Keep `monotone` off.** Use the `drop` flag to audit lakes and diversions. This links to the
   hydro-diversion follow-up in the v3 audit: natural flood statistics overstate diverted reaches such as
   the upper Whanganui, the Moawhango and Waikaremoana.
3. **Leave the Waiokumurau, Waiapu and Karangahake values.** They are high but are H&C's own estimates
   within its ±50 %. A gauge-referenced audit flag could mark them for a person to check.

**What a rebuild to apply it involves.** Routing and tiles are not touched. Set `apply = true` in
`config/maf.toml` so every step picks it up, or pass `--maf-correction on` to each step:

1. `uv run river-finder nz score` (re-scores every national region; writes `segments_scored.gpkg` with
   the corrected `maf_m3s`, `power_kw_m`, `maf_flag`, runs and candidates);
2. `uv run river-finder models table` (`segments_table.pkl`, which now keeps the MAF columns);
3. `uv run river-finder ml table`, then `ml cv` and `ml page` (features and styles from the corrected
   MAF; about 10 min);
4. a new LightGBM card, for example
   `models train --kind gbm --recipe gbm_terrain --name v4`. v3 is frozen and its `pool_quantiles` were
   fitted on raw MAF. Then run `models evaluate` and `ml audit --model v4`;
5. `uv run river-finder score kaimai karangahake tutamoe` (the reference areas; their web app data come from
   `nz viewer` in step 6, since rf-aq6.18 removed the `viewer` page command);
6. `nz viewer`, `models viewer`, `external attach`, `web data`, `web build`, and
   `rain totals --rebuild-network`. The rain network is read from `segments_table.pkl`.

## Rebuild with the correction on (2026-11-24)

The correction was turned on in `config/maf.toml` and every step after routing was rebuilt in the
order above (about 62 min; the national viewer took 29 min of it). Routing, tiles and the reach rows
did not change: the rain network built from the new table is the same as the published one.

**What changed per reach** (1,104,794 national reaches, `maf_flag`):

| flag | reaches |
|---|---|
| none | 973,857 |
| `refill` | 114,685 |
| `drop` | 14,606 |
| `cap` | 1,057 |
| `refill+drop` | 548 |
| `cap+drop` | 28 |
| `refill+cap` | 13 |

**Cross-validation** (`models/cv/results.json`, 5 × 5 catchment-grouped folds), before → after:

| model | auc | auc_size | recall_top10 |
|---|---|---|---|
| flow (log MAF only) | 0.9050 → 0.9100 | 0.540 → 0.573 | 0.729 → 0.739 |
| logit_plus | 0.9316 → 0.9348 | 0.740 → 0.748 | 0.799 → 0.809 |
| gbm_terrain (v3/v4 recipe) | 0.9375 → 0.9372 (sd 0.0017) | 0.789 → 0.783 | 0.808 → 0.810 |

The rebuild agrees with the study above. The logistic models gain because their flood term is less
noisy. The LightGBM model does not change: with gradient, area and geology it had already learned
around the bad values.

**v4 against v3** (`models/evaluations.json`, both scored on the corrected table; held-out split
`guides-v1`, 49 test rivers):

| split | metric | v3 | v4 |
|---|---|---|---|
| guides-v1, any coordinates | auc | 0.9506 | 0.9500 |
| | auc_style_size_matched | 0.854 | 0.852 |
| | recall_top10 | 0.844 | 0.838 |
| guides-v1, sourced coordinates | auc | 0.9734 | 0.9737 |
| | auc_style_size_matched | 0.905 | 0.905 |
| | recall_top10 | 0.924 | 0.918 |
| reference (Mangakarengorengo, 5 m) | auc | 0.9772 | 0.9818 |

The two are equal within noise. v4 is the default model because it is the newest card trained on
all the labels, and its features match the corrected floods that the site now shows. v3 stays in
the registry unchanged. The flood-only baseline gains 0.005 auc on the held-out rivers
(0.9167 → 0.9221) and 0.039 auc_size_matched (0.548 → 0.587).

**Audit lists** (`ml audit`, top 1 % of reaches not in the guides): 46 of the 60 rivers are on both
the v3 and v4 lists. v4 adds, among others, the Rangitata, Waimakariri, Arawata, Matukituki,
Waiohine and Inangahua. It drops, among others, the Tongariro, Ohinemuri, Moeraki and Mikonui
(`models/audits/v3_top_runs.csv`, `models/audits/v4_top_runs.csv`).

**Update, same day (rf-aq6.27):** sections placed by river name now keep one downstream path, so
same-name streams in other valleys no longer count as known runs (13,853 positive reaches in CV
instead of 14,186). With the cleaner labels the CV numbers rise for every model (gbm_terrain auc
0.9372 → 0.9441), and v4 on `guides-v1` reads auc 0.9529 (any coordinates) and 0.9752 (sourced);
v3 reads 0.9538 and 0.9752. The tables above are the numbers before that change.


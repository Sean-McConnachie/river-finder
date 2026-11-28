# Hydro-affected reaches (rf-8x2)

The flood statistics behind `maf_m3s` (NIWA, Henderson & Collins 2018 on REC1) are **natural** flows. Below a
hydro intake most of the water leaves the river, so the natural mean annual flood overstates the flow, and a dam
face or a lake outlet can look like a steep, big-water run. The v3 audit found three such leads in its top list
(the Lake Waikaremoana outlet, the Waipori below Mahinerangi, the lower Wairoa below the Kaimai scheme); the v4
audit's number 1 (the Rangitaiki at the Aniwhenua Falls) is the bypassed reach of the Aniwhenua scheme.

`src/river_finder/hydro.py` flags these reaches from a curated list of schemes, `config/hydro.toml`.

```bash
uv run river-finder hydro check     # where every point snaps, and how far its marking runs
uv run river-finder hydro attach    # data/nz/hydro.pkl (per reach) + data/nz/hydro_summary.json (~30 s)
```

## Classes

| `hydro` | meaning | in the lists |
|---|---|---|
| `diverted` | at least `diverted_share` (0.5) of the natural flow is taken out upstream and not yet returned | left out of `ml audit` and of the Top rivers "New leads" |
| `reservoir` | the lake behind a dam or control structure, and the dam itself | left out, as above |
| `regulated` | taken + controlled flow (storage dam, control gates, tailrace) is at least `regulated_share` (0.5) of the natural flow | kept, labelled |
| `""` | natural (or a smaller share taken) | |

Per reach also `hydro_scheme` and `hydro_point` (the scheme and point that contribute most) and `hydro_share`
(the share behind the class). `[audit] exclude` in the config lists the classes the lists leave out.

## Sources

The schemes, their layout and the operators come from the operator and agency pages listed per scheme in
`config/hydro.toml` (`sources`), and for all of them:

- Electricity Authority, *Hydrological Modelling Dataset, Report 4: Hydro-power Schemes Background and
  Descriptions* (WSP, final report of 11 July 2025; EMI file of 30 Oct 2025),
  [emi.ea.govt.nz](https://www.emi.ea.govt.nz/Environment/Datasets/HydrologicalModellingDataset/1_InfrastructureAndHydroConstraintAttributes/HMD%20Report%204%20-%20Hydro-power%20Schemes%20Background%20and%20Descriptions.pdf),
  with its attributes table (`20241231_InfrastructureAndHydroConstraintAttributes.csv`, same folder) and Report 2,
  *Flow Series Description and Methodology* (same date, `2_Flows_20241231`): "EA Report 4" / "EA Report 2" in the
  config notes, read in rf-6u2 (see below);
- Te Ara, [Hydroelectricity](https://teara.govt.nz/mi/hydroelectricity/print);
  Wikipedia, [Hydroelectric power in New Zealand](https://en.wikipedia.org/wiki/Hydroelectric_power_in_New_Zealand)
  and [List of power stations in New Zealand](https://en.wikipedia.org/wiki/List_of_power_stations_in_New_Zealand);
- per scheme, among others: Genesis Energy ([rivers, lakes and rainfall](https://genesisenergy.co.nz/about/generation/rivers-lakes-and-rainfall):
  Tongariro, Waikaremoana), Horizons Regional Council evidence for Genesis (Tongariro), Engineering NZ heritage
  record (Waikaremoana), Manawa Energy ([Kaimai](https://www.manawaenergy.co.nz/kaimai-power-scheme)), Meridian
  ([Waitaki](https://www.meridianenergy.co.nz/power-stations/hydro/about-the-waitaki-hydro-scheme)), Waikato
  Regional Council (hydro information sheet), Ministry for the Environment fast-track documents (Aniwhenua).

Checked in rf-6u2 (2026-11-27). The operator, Wikipedia and fast-track pages stayed out of reach (the fast-track
site refuses scripts with a Cloudflare challenge; its PDFs were skipped), so:

- **Read directly:** Environment Canterbury consents from its [Open Data Portal](https://data.ecan.govt.nz/)
  (`data/2` consent details with WGS84 positions, `data/4` conditions, `data/80` allocations by `RecordNo`): Lake
  Coleridge (CRC173079 Harper Diversion, CRC173064 and CRC173021 Oakden Bank, CRC172958 Wilberforce, CRC172988,
  CRC173092 and CRC173100 Lake Stream) and the Rangitata Diversion Race (CRC182542); the national 1 m and Kaimai 5 m
  LiDAR DEMs (levels, weir ponds) and the 8 m network (catchment areas).
- **Read directly, second check (Electricity Authority, EMI files served from `emidatasets.blob.core.windows.net`):**
  Report 4 (2025; its 2018 and 2022 editions carry the same scheme text), the attributes table, Report 2 and the
  Electricity Commission's *Lake Level History* (Opus, 27 Feb 2009). They describe the schemes' layout and history;
  they give no intake rate, residual flow, level or share for the Kaimai intakes, Wilberforce, the Rangitata
  Diversion Race or Piriaka (no Piriaka section), and no intake positions (the figures are location diagrams with
  flow sites, a Coleridge layout sketch and the Genesis Tongariro schematic).
- **Through web-search excerpts only:** the Kaimai re-consenting documents on fasttrack.govt.nz (Appendix A final
  conditions, Appendix F existing consents, Appendix H Tonkin & Taylor hydrology, the January 2026 panel presentation)
  and NIWA Fisheries Environmental Report 22 (Piriaka). Values taken from them are cross-checked where possible: the
  Opuiaki Weir's consent map reference (N67:483343, NZGD49 North Island Grid) lands 60 m from where the LiDAR DEM has
  the river at the weir's crest level with the catchment Tonkin & Taylor give.

What the Electricity Authority documents changed or confirmed (section and page in the config notes):

| value | verdict | from |
|---|---|---|
| Kawarau gates, `take` 0.3 (estimate) | **corrected to 0**: "while Lake Wakatipu has control gates at its outlet, these were built for mining purposes and are very seldom used. Therefore, only Lake Hāwea is controlled and outflows from the other two lakes are classed as tributary flows"; the attributes table has Wakatipu as a run-of-river reservoir (Hawea: controlled); the Lake Level History counts Wakatipu among "the natural Lakes", gates used 1926-38 and 1941-56 | Report 4, 3.4, p. 20; attributes CSV; Lake Level History 3.6.1, Table 3.8 |
| Kawarau note: "the gates hold water back in floods" (excerpt) | **corrected**: the Lake Level History says that of the Taupo gates (2.1.1), not of Wakatipu's | Lake Level History |
| Lake McLaren dam as the Ruahihi Canal intake | **confirmed**: "a 26 m high concrete arch dam across the lower Mangapapa River"; "a gated inlet structure into a 2.5 km canal" | Report 4, 2.8, p. 10 |
| Ruahihi as the scheme's return to the Wairoa | **confirmed**: on the Wairoa by SH29, 2 x 10 MW at 86.4 m head | Report 4, 2.8, p. 10 |
| Kaimai dam-release run below McLaren | **confirmed in kind** ("recreational flows into the Wairoa River on set days each year"), not the number of days | Report 4, 2.8, p. 10 |
| Kaimai stations | Lloyd Mandeno 1972, Lower Mangapapa 1979, Ruahihi 1981 and **Kaimai 5 1994** (no position: not modelled) | Report 4, 2.8 |
| Rangitata Diversion Race intake at Klondyke | **confirmed**: Figure 3-9 draws the race from just below flow site 69302 Rangitata at Klondyke, with 69304 Mayfield/Hinds at Intake on the race | Report 4, 3.7, p. 23 |
| Lake Coleridge layout (Harper Diversion above the Oakden Canal intake, Wilberforce Canal intake, outlet gate) | **confirmed**: Figure 3-8; the outlet is "regulated by a control gate", spill seldom; the Harper and Wilberforce bunds wash out in floods | Report 4, 3.6, p. 22; Report 2, 2.2.6, p. 22 |
| Taurewa and Tawhitikuri intakes | **exist**, on the Western Diversion between the Okupata and Mangatepopo intakes (Figure 2-3, Genesis schematic, not to scale); positions not given | Report 4, 2.2, p. 4 |
| Monowai: intake at the lake outlet (approx) | **contradicted**: gates at the outlet, then about 6 km of river to the canal's weir; modelled so since the 1 m network (rf-73f: outlet `dam`, weir `intake`, see Limits) | Report 4, 3.3, p. 19, Figure 3-5 |

**Unconfirmed (as of rf-6u2, after the Electricity Authority check):** the Kaimai intake rates, residual flows,
crest levels, catchments and the Mangakarengorengo spill days (search excerpts of the fast-track documents; Report 4
names none of the intakes); the Opuiaki consent position and the Ngatuhoa and Mangakarengorengo weir positions
(approx, ±100-200 m); the Ruahihi Canal rate (31.2 m3/s), the Lloyd Mandeno Canal rate (14.2 m3/s) and the 150 l/s
and 26 release days below McLaren; the partial takes of Wilberforce (0.85), the Rangitata Diversion Race (0.25) and
Piriaka (0.6, and its weir position); the Taurewa and Tawhitikuri positions (not modelled). Each keeps its `approx`,
estimate or excerpt mark in the config.

Positions: dams, weirs and power stations mapped in OpenStreetMap, read from the Overture Maps release
`2026-10-21.0` (`base/infrastructure`: `water/dam`, `water/weir`, `power/plant`, `power/generator` with a hydro
source tag; about 390 dams, 470 weirs and 230 hydro plants and generators in NZ). Points without an OSM feature
(lake outlets, some intakes) are placed on the named river from the scheme description and marked `approx` in the
config. Licence of the OSM positions: ODbL, © OpenStreetMap contributors.

## Coverage

24 schemes, 81 points: Tongariro Power Scheme (Whanganui, Te Whaiau, Otamangakau, Whakapapa, Mangatepopo and Okupata
intakes; Moawhango and Rangipo dams; Rangipo tailrace; Poutu Intake), Waikaremoana, Kaimai (Opuiaki and Ngatuhoa
weirs, Lake Mangaonui, Mangapapa weir on the upper Wairoa, Omanawa intake, Lloyd Mandeno, Mangakarengorengo weir,
Lake McLaren and Ruahihi), Aniwhenua and Matahina, Wheao and Flaxy, the Waikato system (Taupo gates, Aratiatia,
Ohakuri, Atiamuri, Whakamaru, Maraetai, Waipapa, Arapuni, Karapiro), Piriaka, Patea, Mangahao, Cobb, Lake Coleridge
(Harper Diversion, Oakden Bank, Lake Stream dam, Wilberforce diversion), Highbank and the Rangitata Diversion Race,
Opuha, Waitaki (Tekapo, Pukaki and Ohau control structures, Ohau A and C, Ruataniwha, Benmore, Aviemore, Waitaki),
Clutha (Clyde, Roxburgh, Hawea and Kawarau controls), Waipori, Manapouri (Te Anau gates, Mararoa Weir), Monowai,
Arnold, Kumara, Kaniere, Roaring Meg, Branch River and Waihopai.

Not covered (small or no position found): the 22 Whangaehu-side intakes of the Eastern Diversion, the Waihohonu
intake (Tongariro section, EA Report 4 2.2), the Taurewa and Tawhitikuri intakes (no source gives their position; the
Tawhitikuri Stream is named in REC2, up to 8.4 km², and no stream is named Taurewa in REC2 or the LINZ Topo50 names),
the Kaimai scheme's Tauwharawhara take (283 l/s), Lake Matariki and Lower Mangapapa bypass, Kaimai 5 (1994, EA Report 4
gives no position), the Acheron diversion to Lake Coleridge, Mangorei, Motukawa, Hinemaiaia, Kuratau, Wairere, Matiri,
Paerau and Patearoa, Teviot, Fraser, Wye Creek, Amethyst, Wahapo, Inchbonnie and the irrigation storages (Falls Dam,
Waimea).

Counts after `hydro attach` (2026-11-25, national table): diverted 1,859 reaches / 454 km, regulated 3,434 / 834 km,
reservoir 2,588 / 628 km (500 km on reaches of 2 km² or more). Per scheme: `data/nz/hydro_summary.json`.
With the rf-6u2 config (`hydro check`, 2026-11-27, same table; `hydro attach` not yet rerun): diverted 2,069 / 505 km,
regulated 3,441 / 836 km, reservoir 2,593 / 629 km. Kaimai: diverted 99 → 190 reaches (24 → 47 km), regulated
66 → 73, reservoir 15 → 20; Lake Coleridge: diverted 1 → 120 reaches (0.3 → 29 km); the other schemes are unchanged.
The Electricity Authority check (Kawarau gates `take` 0.3 → 0, notes and sources) changes no class, scheme, point or
share (`hydro check` before and after: `data/analysis/hydro-6u2/ea2_*`).

## Method

1. **Snap.** Each point goes to the nearest reach of its named river (REC2 name, `models.norm_river`) within
   `snap_named_m` (3 km); `river = ""`, or no named reach that close, takes the nearest reach of 1 km² or more
   within `snap_any_m` (700 m). The same `models._Index.nearest` places the guide sections.
2. **Walk down.** From each point, in upstream-to-downstream order, the downstream path gets the point's
   catchment area: an `intake` adds `take` × the natural flow still in the river (its area minus what is already
   taken upstream) to the reaches' *diverted* area, until its water returns (the first reach on the path of a
   `return` point of the scheme that carries it), the walk enters the river named in `until` (a confluence) or
   `km` is reached. A `dam` adds the flow it controls to the *regulated* area; a `return` adds the water it brings
   back (unless `regulates = false`: a run-of-river bypass, natural again below). Shares are these areas over
   the reach's own catchment, summed over every point upstream: several intakes on one river add up (the Western
   Diversion on the upper Whanganui), a diversion fades as tributaries join, and nested catchments are not
   counted twice.
3. **Reservoirs.** For a point with `reservoir = true`: the reaches within `dam_m` (300 m) below it and
   `dam_up_m` (1.5 km) above it (the smoothed gradient spreads the dam's drop over its neighbours), and every
   reach upstream flatter than `reservoir_grad` (0.5 m/km: lakes are flat in the DEM) within `reservoir_km`.

## Where it is used

- `ml audit` (`ml.candidate_runs`): reaches of the `[audit] exclude` classes never start, join or bridge a run.
  They stay in the percentile reference, so the other reaches keep their percentiles. The CSV gains `hydro`,
  `hydro_scheme` and `hydro_frac` (the class over at least half the run), and the log lists what was left out.
- `web data --only hydro` writes `<region>_hydro.json` (`webdata.hydro_record`): flagged viewer reaches with
  class, scheme (name, operator, note, sources) and share. The **Top rivers** page labels runs ("Hydro-diverted",
  "Regulated flow", "Hydro lake or dam" with the scheme) and files runs not in the guides that lie mostly on
  excluded classes under a fourth status, "Hydro-affected", instead of "New leads" ("New leads only" hides them).
  The map's **details** view shows the pill and a note with the scheme and a source link.
- ML features and labels do not use it.

## Limits

- **Coverage.** Only the listed schemes; small schemes and many minor intakes are missing, and some intakes and
  lake outlets are approximate (`approx` in the config). `hydro check` shows each snap.
- **Shares by catchment area, not flow.** An intake takes `take` of the flow by area. Intake capacities are
  finite: in a flood most intakes spill, so the flood itself is less reduced than the share says; the share
  describes normal flows, which is what makes a reach runnable. `take = 1` where the intake takes all but a residual
  flow at normal flows, e.g. the Mangakarengorengo weir: up to 7 m³/s of a 6.55 m³/s mean flow, but it spills on
  about 103 days a year, so on average it takes 53 %. Partial takes (`take` < 1) remain estimates, each with its
  reason in the point's note: Wilberforce 0.85 (consent: up to 30 m³/s, at least 3 m³/s left; the river's flow
  there not found), Piriaka 0.6 (station capacity and the river's median flow not found), Rangitata Diversion Race
  0.25 (the consent's restriction table keeps the share at or below about 0.5 at every flow); the Electricity
  Authority reports give no flows for any of them. The Harper Diversion takes the whole river (40 m³/s, no residual
  flow): `take = 1` since rf-6u2. The Kawarau gates control nothing at normal flows (EA Report 4: "very seldom
  used"): `take = 0` (an estimate of 0.3 until rf-6u2); the point stays listed for coverage.
- **The 0.5 thresholds** put a reach just under half diverted outside `diverted`. The Wairoa below Lake McLaren
  was such a case until rf-6u2, for a wrong snap: the dam point took the Mangakarengorengo's last reach (114 km²,
  REC2 names it Wairoa River) instead of the lake's outflow (167 km², named Ruahihi Canal in REC2). With the outflow
  and the Mangakarengorengo weir the reaches from the dam to Ruahihi are 0.91 to 0.98 diverted.
- **Shares above 1.** Water that a return brings back is added again where a later return releases it (Kaimai:
  the Lloyd Mandeno release passes Ruahihi), so `reg_share` can pass 1 below a chain of stations; `hydro_share` is
  capped at 1 and the class is unaffected.
- **Network.** Points snap to the 8 m network. Where it is wrong the marking is too: the upper Waikato between
  Lake Taupo and Ohakuri is unnamed and does not carry Lake Taupo's catchment (3 km² at the Taupo gates), so the
  Taupo gates carry `area_km2 = 3300`. Lake reaches are counted as the DEM's flow lines across the lake.
  **Monowai (open, found in rf-6u2):** EA Report 4 (3.3, p. 19, Figure 3-5) has gates at the Lake Monowai outlet and
  "Water flows down the river for about 6 km into a lake formed by a weir across the river", then an 856 m canal and
  a 1,036 m pipeline to the powerhouse: only the river below the weir is bypassed (OSM maps the weir, from LINZ
  Topo50, at -45.77995, 167.59121, where its canal starts). The 8 m network instead routes the lake's outflow (231 km²)
  north over a steep reach that no LINZ or REC2 line follows into the Borland Burn, and carries the real Monowai (REC2:
  243-280 km²) at only 2-14 km². The 1 m network (rf-lxa.10) follows the Monowai to the Waiau (249 km² at the weir,
  REC2 278), so since rf-73f the outlet is a `dam` (the river below regulated) and the weir an `intake` (the river
  below it diverted to the station's return by the Waiau).
- **Rebuilds.** `hydro.columns` uses `data/nz/hydro.pkl` while it lines up with the table (same reaches, same
  order) and recomputes from the config otherwise; run `hydro attach` again after a national rebuild or a config
  change.

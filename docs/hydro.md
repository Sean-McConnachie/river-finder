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

- Electricity Authority, *Hydrological Modelling Dataset, HMD Report 4 - Hydro-power Schemes Background and
  Descriptions* (EMI file of 30 Oct 2025),
  [emi.ea.govt.nz](https://www.emi.ea.govt.nz/Environment/Datasets/HydrologicalModellingDataset/1_InfrastructureAndHydroConstraintAttributes/HMD%20Report%204%20-%20Hydro-power%20Schemes%20Background%20and%20Descriptions.pdf)
  (a general reference: no value in the config is read from it, see below);
- Te Ara, [Hydroelectricity](https://teara.govt.nz/mi/hydroelectricity/print);
  Wikipedia, [Hydroelectric power in New Zealand](https://en.wikipedia.org/wiki/Hydroelectric_power_in_New_Zealand)
  and [List of power stations in New Zealand](https://en.wikipedia.org/wiki/List_of_power_stations_in_New_Zealand);
- per scheme, among others: Genesis Energy ([rivers, lakes and rainfall](https://genesisenergy.co.nz/about/generation/rivers-lakes-and-rainfall):
  Tongariro, Waikaremoana), Horizons Regional Council evidence for Genesis (Tongariro), Engineering NZ heritage
  record (Waikaremoana), Manawa Energy ([Kaimai](https://www.manawaenergy.co.nz/kaimai-power-scheme)), Meridian
  ([Waitaki](https://www.meridianenergy.co.nz/power-stations/hydro/about-the-waitaki-hydro-scheme)), Waikato
  Regional Council (hydro information sheet), Ministry for the Environment fast-track documents (Aniwhenua).

Checked in rf-6u2 (2026-11-27). The session could not open the Electricity Authority, operator, Wikipedia or
fast-track pages (outside its network allowlist), so:

- **Read directly:** Environment Canterbury consents from its [Open Data Portal](https://data.ecan.govt.nz/)
  (`data/2` consent details with WGS84 positions, `data/4` conditions, `data/80` allocations by `RecordNo`): Lake
  Coleridge (CRC173079 Harper Diversion, CRC173064 and CRC173021 Oakden Bank, CRC172958 Wilberforce, CRC172988,
  CRC173092 and CRC173100 Lake Stream) and the Rangitata Diversion Race (CRC182542); the national 1 m and Kaimai 5 m
  LiDAR DEMs (levels, weir ponds) and the 8 m network (catchment areas).
- **Through web-search excerpts only:** the Kaimai re-consenting documents on fasttrack.govt.nz (Appendix A final
  conditions, Appendix F existing consents, Appendix H Tonkin & Taylor hydrology, the January 2026 panel presentation),
  NIWA Fisheries Environmental Report 22 (Piriaka) and the Electricity Commission lake level history (Kawarau Falls).
  Values taken from them are cross-checked where possible: the Opuiaki Weir's consent map reference (N67:483343, NZGD49
  North Island Grid) lands 60 m from where the LiDAR DEM has the river at the weir's crest level with the catchment
  Tonkin & Taylor give.
- **Electricity Authority Report 4: not read.** A second check (2026-11-27, with www.emi.ea.govt.nz allowed) found
  the report in the EMI folder listing (`1_InfrastructureAndHydroConstraintAttributes`, the 30 Oct 2025 file; the
  `_2022.pdf` link of rf-8x2 answers 404), but EMI serves the file, the archived 2017-2021 editions and the
  attributes CSV by a redirect to `emidatasets.blob.core.windows.net`, which the session cannot reach. Web-search
  excerpts of the report gave no scheme values. The fast-track PDFs were skipped (a Cloudflare challenge refuses
  scripts). No value was confirmed or changed.

**Unconfirmed (as of rf-6u2):** the Kaimai intake rates, residual flows, crest levels, catchments and the
Mangakarengorengo spill days (search excerpts of the fast-track documents); the Ngatuhoa and Mangakarengorengo weir
positions (approx, ±100-200 m); the partial takes of Wilberforce (0.85), the Rangitata Diversion Race (0.25), the
Kawarau gates (0.3) and Piriaka (0.6, and its weir position); the Taurewa and Tawhitikuri intakes (not modelled).
Each keeps its `approx`, estimate or excerpt mark in the config.

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

Not covered (small or no position found): the 22 Whangaehu-side intakes of the Eastern Diversion, the Taurewa and
Tawhitikuri intakes (no source gives their position; the Tawhitikuri Stream is named in REC2, up to 8.4 km², and no
stream is named Taurewa in REC2 or the LINZ Topo50 names), the Kaimai scheme's Tauwharawhara take (283 l/s), Lake
Matariki and Lower Mangapapa bypass, the Acheron diversion to Lake Coleridge, Mangorei, Motukawa, Hinemaiaia,
Kuratau, Wairere, Matiri, Paerau and Patearoa, Teviot, Fraser, Wye Creek, Amethyst, Wahapo, Inchbonnie and the
irrigation storages (Falls Dam, Waimea).

Counts after `hydro attach` (2026-11-25, national table): diverted 1,859 reaches / 454 km, regulated 3,434 / 834 km,
reservoir 2,588 / 628 km (500 km on reaches of 2 km² or more). Per scheme: `data/nz/hydro_summary.json`.
With the rf-6u2 config (`hydro check`, 2026-11-27, same table; `hydro attach` not yet rerun): diverted 2,069 / 505 km,
regulated 3,441 / 836 km, reservoir 2,593 / 629 km. Kaimai: diverted 99 → 190 reaches (24 → 47 km), regulated
66 → 73, reservoir 15 → 20; Lake Coleridge: diverted 1 → 120 reaches (0.3 → 29 km); the other schemes are unchanged.

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
  0.25 (the consent's restriction table keeps the share at or below about 0.5 at every flow) and Kawarau gates 0.3
  (no consent terms found). The Harper Diversion takes the whole river (40 m³/s, no residual flow): `take = 1`
  since rf-6u2.
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
- **Rebuilds.** `hydro.columns` uses `data/nz/hydro.pkl` while it lines up with the table (same reaches, same
  order) and recomputes from the config otherwise; run `hydro attach` again after a national rebuild or a config
  change.

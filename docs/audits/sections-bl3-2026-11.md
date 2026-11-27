# Audit: guide sections that map loosely onto the reaches (rf-bl3, 2026-11-27)

The 346 guide sections (339 in `labels/guide_research.json`, 7 in `labels/known_runs.toml`) become
training labels through `models.map_section`. Many of them did not land on the river they describe.
This note says what was wrong, what changed, and what is left.

## How it was measured

Every section from `models.all_sections()` was placed with `models.map_section` on today's 8 m national
table (`data/nz/segments_table.pkl`, read only), as `ml table`, `models train` and the web app do. Per
section: the method, the mapped km, the stated km, and what the name rule dropped (`labels.one_path`).
A problem is one of:

- **no reach**: nothing placed;
- **fallback**: "name near the coordinates (path did not join)", i.e. the river's steep reaches within
  15 km of the points, because the put-in and take-out did not join down the network;
- **length off**: mapped km below half or above 1.5 times the stated km;
- **pieces dropped**: the name rule found more than one piece or branch and kept one.

The same was done on the 1 m sample (`data/nz1m_sample/finish_full`, tiles q24/q25 y32/y33,
Kaimai to Tauranga) for the sections with a point in it. Outputs (not committed):
`data/analysis/sections-bl3/{before,after}_8m.csv` and `{before,after}_1m_sample.csv`.

## What was wrong

1. **Rounded points.** Most packraftingtrips.nz points were rounded to 0.1° (low accuracy). Many were
   10 to 30 km off: on another river (Thomas on Mather Creek, Nina on Goings Creek, South Temple on the
   North Branch), or off any river (Huxley, Cromel, Windley, Winton Burn, Worsley).
2. **Guessed take-outs.** Most riverguide take-outs were inferred. Several sat on side creeks (Ngamuwahine
   on the Mangatarata, Waitara on the Parahaki), so the path did not join.
3. **Take-outs on another river.** A point snaps to the nearest reach with the section's name within
   3 km. A take-out on the main river below a confluence snapped back up the section's own river
   (Whakapapanui and Whakapapaiti to Whakapapa Dam: 2.6 and 5.4 km instead of 6 and 9).
4. **Names.** REC2 leaves some reaches unnamed (the Mōkau at Wairere Falls) or spells them differently
   ("Windon Burn", "Tuakopae").
5. **Missing points.** Some riverguide records have one point and no take-out or length.

## Code change

`models.map_section` now reads two optional label fields, `put_in_river` and `take_out_river`
(`models.snap_river`): the name a point snaps to, or `""` for the nearest reach whatever its name.
They are set only where a label needs them (5 sections). Without them nothing changes.
Test: `tests/test_models.py::test_map_section_snap_river_override`.

## Result

8 m table, all 346 sections:

| Count | Before | After |
|---|---|---|
| no reach | 9 | 1 |
| fallback (name near the points) | 47 | 18 |
| length off by more than 50 % | 45 | 34 |
| pieces or branches dropped | 31 | 17 |
| any of these | 93 | 57 |
| placed put-in to take-out | 237 | 271 |

1 m sample (15 sections before, 16 after; the Whatakao's new put-in is at the edge of the tiles):
fallback 3 → 3, length off 4 → 2, pieces dropped 2 → 1, any 7 → 5. The Ngamuwahine (2.0 km) and
Tuakopai (5.8 km) now place put-in to take-out on the 1 m network too, and McLaren Falls to
Powerstation is 2.8 km (was 4.9). The Whatakao take-out is outside the sample, so it falls back there.

## Fixed sections (45, all in `guide_research.json`)

Each entry has new `*_coord_source` and `*_coord_accuracy` text and a `data_notes` line starting
"rf-bl3". Sources: the NZ RiverGuide API (`api.riverguide.co.nz/guides`, one record now has a point
it lacked) and its descriptions; DOC hut and campsite grid references (NZTM2000) from the DOC pages;
Mountain-Forecast rounded positions where DOC gave none (marked medium accuracy); LINZ waterfall points
(`data/nz/waterfalls.gpkg`) and LINZ river name lines (`data/nz/linz/`); OSM roads and tracks
(Overture, `data/nz/access.gpkg`) where they cross the modelled river; confluences on the 8 m reach
network. "Before" and "After" give the method (path = put-in to take-out, towards = put-in walked
towards the take-out, near points = fallback) and the mapped km.

| River | Section | Changed | Source | Before (km) | After (km) | Stated km |
|---|---|---|---|---|---|---|
| Awatere | Mt Gladstone to Awapiri | take-out | riverguide text, OSM x river, packraftingtrips.nz | path 7.9 | path 21.2 | 20 |
| Broken River | Cave Stream to Avoca / Waimakariri | put-in, take-out | 8 m network | near points 18.6 | path 21.4 |  |
| Buller River | O'Sullivan's to Ariki | take-out | LINZ waterfall | path 9.8 | path 5.6 | 5.9 |
| Catlins River | Chloris Stream to Tawanui campsite | put-in, take-out | DOC grid ref, LINZ river lines | near points 11.1 | towards 13.6 |  |
| Cromel Stream | Cromel Branch Hut to Selbie Rd bridge | put-in, take-out (Base Hut) | DOC grid ref, packraftingtrips.nz | no reach | path 9.9 |  |
| Crooked River | Upper Upper Gorge | take-out | next riverguide section | near points 17.1 | path 5.9 |  |
| Dingle Burn | Below Top Dingle Hut to Lake Hawea | put-in, take-out | Mountain-Forecast (rounded), 8 m network | near points 23.9 | path 31.3 | 30 |
| Eglinton River | Cascade Creek to Mistake Creek | put-in, take-out | DOC campsite listing, OSM x river | near points 19.3 | path 4.5 |  |
| Fox | Fox Glacier to SH6 | take-out | OSM x river | path 4.8 | path 4.1 | 3 |
| Grey River | Robinson junction to SH7 near Waipuna Rd | put-in, take-out | OSM x river, 8 m network | near points 2.2 | path 48.6 | 50 |
| Hawdon River | Discovery Stream to Hawdon shelter | put-in, take-out | DOC grid ref, LINZ river lines | near points 8.7 | path 9.6 |  |
| Huxley River | Huxley Forks to Hopkins | put-in, take-out | DOC grid ref, 8 m network | no reach | path 6.9 |  |
| Karangarua River | Cassel Flat to Copland confluence | put-in, take-out | Mountain-Forecast (rounded), OSM x river, 8 m network | near points 9.2 | path 7.0 |  |
| MacFarlane | Blackfoot Creek to Landsborough | take-out | 8 m network | near points 13.3 | path 13.4 |  |
| Mangaotaki | SH3 to Mokau | take-out | riverguide text, OSM x river | near points 8.0 | path 6.4 |  |
| Maruia River | Cannibal Gorge to Maruia Springs | put-in | OSM x river | no reach | path 8.1 |  |
| Mistake Creek | Mistake Creek to SH94 | put-in removed, take-out | OSM x river | near points 6.2 | near points 6.2 |  |
| Mohaka | Pakaututu to SH5 | take-out | riverguide text, OSM x river | near points 10.1 | path 15.9 |  |
| Mōkau | Totoro Gorge | take-out, `put_in_river` "" | OSM x river | no reach | path 14.8 |  |
| Ngamuwahine River | Ngamuwahine | take-out | riverguide text, OSM x river | near points 8.6 | path 2.2 |  |
| Nina River | Nina Hut to Lewis River | put-in, take-out | DOC grid ref, 8 m network | near points 10.3 | path 7.7 |  |
| Okuru River | Ngatau confluence to Jackson Bay Rd bridge | put-in | 8 m network | near points 1.6 | path 37.7 |  |
| Parapara | Richmond Flat | take-out | riverguide text, OSM x river | path 4.7 | path 4.5 | 15 |
| Paringa River | Tunnel Creek Hut to SH6 bridge | put-in, take-out | DOC grid ref, OSM x river | near points 0.5 | path 16.3 |  |
| Perth | Five Finger Stream to old SH6 bridge site | take-out | riverguide text, OSM x river | towards 15.9 | path 13.5 |  |
| Perth | Scone Hut to old SH6 Bridge Site | take-out | riverguide text, OSM x river | towards 22.4 | path 20.0 | 13 |
| Perth | Upper Perth | take-out | riverguide text, OSM x river | towards 25.1 | path 22.8 | 13 |
| Polnoon Burn | Blue Creek to Branches Station | put-in, take-out | LINZ river lines | near points 20.6 | path 11.7 |  |
| Poutu Stream | Poutu Stream (the falls) | put-in, take-out | LINZ waterfall | near points 0.2 | path 0.2 |  |
| South Temple Stream | South Temple Hut to Hopkins Valley road bridge | put-in, take-out | DOC grid ref, OSM x river | near points 5.8 | path 7.4 |  |
| Tarawera River | Lake Tarawera to Tarawera Falls | take-out | LINZ waterfall | path 1.4 | path 3.8 | 7 |
| Thomas River | Thomas River Hut to Haast River | put-in, take-out | DOC grid ref, 8 m network, packraftingtrips.nz | towards 15.1 | path 7.1 | 18 |
| Tuakopai River | Tuakopai | put-in, take-out, `put_in_river` "Tuakopae Stream" | riverguide text, OSM x river | towards 1.0 | path 5.7 | 5.5 |
| Waiho River | Franz Josef Glacier to SH6 | take-out | OSM x river | path 6.8 | path 6.6 | 4.5 |
| Waikato | Huka Falls | take-out | riverguide text, LINZ waterfall | towards 1.4 | path 0.5 | 0.5 |
| Waingawa River | Waingawa | take-out | riverguide text, 8 m network | near points 18.6 | path 2.6 |  |
| Wairoa River | McLaren Falls to Powerstation | take-out | known_runs.toml fix (rf-aq6.27) | path 5.6 | path 2.8 | 2.7 |
| Waitara | Everett Road to Bertrand Road | take-out | OSM x river | near points 1.2 | path 8.5 |  |
| Whakapapaiti | SH47 to Whakapapa Dam | `take_out_river` "Whakapapa River" | REC2 names | path 5.4 | path 9.3 | 9 |
| Whakapapanui | Matariki Falls to Whakapapa Dam | `take_out_river` "Whakapapa River" | REC2 names | path 2.6 | path 6.2 | 6 |
| Whatakao | Whatakao River | put-in, take-out | riverguide text (written coordinates) | near points 8.1 | path 8.5 |  |
| Whataroa River | Butler Hut to old SH6 bridge site | put-in, take-out | riverguide API, OSM x river | no reach | path 16.4 | 16.8 |
| Windley River | Lower Windley Hut to Oreti confluence | put-in, take-out | Mountain-Forecast (rounded), 8 m network | no reach | path 6.5 |  |
| Winton Burn | Above Forks Hut to North Mavora Lake | put-in, take-out, `*_river` "Windon Burn" | DOC grid ref, 8 m network | no reach | path 6.7 | 8 |
| Worsley Stream | Above Castle River confluence to Lake Te Anau | put-in, take-out | DOC grid ref, 8 m network | no reach | path 5.8 | 2 |

Points that end a trip on a bigger river (Huxley at the Hopkins, Thomas at the Haast, Winton Burn at the
Mararoa, Polnoon at the Shotover) stop at the confluence: the float down the main river is not
whitewater and its take-out was not pinned. Mistake Creek keeps no put-in ("near the 600 m contour"
needs contours); it is placed by name near its corrected take-out.

## What remains

**No reach or fallback (18)**, unresolved:

- Waikato, Control Gates to Reids Farm: no reach. The 8 m network has no Waikato reach at the Lake
  Taupō outlet. A network gap, not a label problem.
- Taramakau, Jacksons to Kumara: the 8 m network routes the Taramakau at Inchbonnie into Lake Brunner
  (an old overflow path), so no path reaches Kumara. A network problem; check on the 1 m rebuild.
- One riverguide point and no take-out or length, placeholder descriptions: Waikaretaheke (Piripaua),
  Makuri Gorge, Waipoua, Ohinepango, Pokaiwhenua, Waihou near Putaruru, Tekapo (SH8 to Tekapo A, the
  power station was not located). Rakaia Gorge: its one point is the SH77 bridge, the take-out.
- Mangaotaki bridge to SH3: the riverguide point lies in the Marokopa catchment (Pomarangai Stream),
  6 km from the Mangaotaki. The "Mangaotaki Bridge" on the Waitanguru road could not be pinned.
- Matukituki (three sections): Cameron Flat, Junction Flat and Aspiring Hut have no coordinates in the
  sources found.
- Harrison (Milford Sound out-and-back), Kaipo (off-track route), Cascade (Olivine Wilderness boundary):
  no locatable put-in.
- Mistake Creek: see above.

**Length off (34)**. 13 are explained, mostly in the entries' `data_notes`: the stated km is the gorge
only (Kakapotahi, Morgan Gorge, Maruia Falls), leaves out the float to the take-out (Perth ×2,
Waipara, Mason), or does not fit the published points or the map (Acheron, Arahura, Parapara, Worsley,
Thomas; Nevis Bluff, where the text puts the take-out 2.5 km down). The Gowan stops 4 km below Lake
Rotoroa because the network breaks there. The other 20 have inferred take-outs that no source pins
down: Ruakituri (3), Waipunga, Tokomaru, Waikawa, West Akatarawa, Boyle, Wairau (Hell's Gate), Lochy,
Maitai, Matakitaki (2), Waingaro, Hokitika (Kakariki), Ngakawau, Toaroha (2), Tarawera (Wooden Rd to
Concrete Bridge), Mangakarengorengo (Ngamuwahine Bridge to McLaren Falls).

**Needs the user.** The reference runs in `known_runs.toml` without points are placed by name: Waima
(keeps 3.9 of 16.3 km of steep "Waima River"; 12.4 km in other pieces dropped), Mangakahia (1 of 2
pieces, 1 of 3 branches), Waipoua (1 of 2 branches), Mangakarengorengo (1 of 2 pieces), Tuakopae, and
the research's Wainui flood run. Only the user knows their extents: a put-in and take-out (or outlet
and length) would fix them.

## Retraining

The labels feed card v5 when `river-finder nz1m finish` runs (docs/nz1m-finish.md). Old splits and
cards are frozen; river keys can change where a put-in moved to another guide region, so use a new
split for the retrain.

## Follow-up: fork names (rf-r4a, 2026-11-27)

`models.norm_river` drops the kind of water, macrons, parentheses and the fork words (north, south, east,
west, left, right, middle, branch), so "Matukituki River East Branch", "Matukituki River (West)" and
"Matukituki River" share one name. That stays: it is what lets a guide's "Matukituki" find REC2's branches.
What changed:

- **Fork words.** `models.river_side` keeps them ("east"). A section whose river name names a fork
  ("Matukituki River (East)", "South Temple Stream", "West Akatarawa"), or whose title does ("West Branch
  Waikaia", "North Branch (Johnson Hut to Forks)"), matches reaches of that fork or of the main stem, never
  another fork (`models.section_side`, `same_side`). Its points cannot snap to the other fork and the name
  rule does not take it. A section with no fork word matches every fork, as before.
- **Names made only of such words** ("Branch River", "North Stream", "Middle Creek") normalised to "", so
  they counted as unnamed. They now keep the words ("branch"). `_Index` builds its names from `river` with
  this code, not from the cached `rnorm` column, so today's table gets the fix too. Hydro points snap as
  before (all 75 checked).
- **Aliases** (`models.RIVER_ALIASES`) for guide spellings REC2 writes differently: Routeburn / Route Burn,
  Tauwharenikau / Tauherenikau, Hangatuha (Stony River) / Stony River. Not Makawhio / "Jacobs (Makawhio)
  River": REC2's name stops 2.5 km above the SH6 take-out, so the take-out would snap 2.5 km up the river
  (9.6 km instead of 13.3 km, stated 15 km).

Tests: `tests/test_models.py::test_norm_river_forks_and_aliases`, `test_map_section_keeps_the_named_fork`.

On today's 8 m table 3 of 346 sections change, none for the worse: Tauwharenikau (same reaches, now
put-in to take-out instead of walked towards the take-out) and Matukituki East and West (same reaches; the
other fork is no longer a candidate, so the name rule drops 0.5 and 0 km instead of 10.1 and 7.7 km). Most
fork mix-ups the issue saw (South Temple on the North Branch, the Matukituki forks) were already fixed by
rf-bl3's points; the rule keeps them from coming back when a point is rough. The 1 m sample does not change.

**Not done: splitting same-valley pieces into separate sections.** After rf-bl3 the Grey and the Waitara are
placed by their points, not by name. Same-valley pieces more than 10 km apart remain only on the Hokianga
Waima and the Mangakahia: in both, a 0.7 km piece of the big river 12.5 km below the run (430 to 510 km2,
near the Waima's mouth). As separate sections they would label lowland river as runs, so the name rule
still keeps one path. The Waima's other dropped piece (11.7 km) is another Waima that drains to the
Northern Wairoa. The extents of these reference runs still need the user.

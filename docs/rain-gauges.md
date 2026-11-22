# Rain gauges (rf-t26.4)

Hourly rain-gauge totals from the regional councils' gauge servers, cached locally, for the gauge
scale factors of rf-t26.5 (grid totals vs gauge totals per source and region over a rolling 30 days).
First councils: West Coast (WCRC) and Tasman (TDC) through their Hilltop servers, Canterbury (ECan)
through its Open Data Portal.

Code: `src/river_finder/rainfall/hilltop.py` (Hilltop client, XML, time zones, choice of rain series),
`src/river_finder/rainfall/ecanportal.py` (ECan's portal) and `src/river_finder/rainfall/gauges.py`
(hours, quality states, placement, cache, outputs; its docstring is the reference). Config: `[gauges]`
in `config/rainfall.toml`. Tests: `tests/test_rainfall_gauges.py`; fixtures in `tests/fixtures/hilltop/`
(synthetic, plus three real WCRC/TDC answers) and `tests/fixtures/ecan/` (real, trimmed).

## Status: verified live (2026-11-21)

From the development container, `rain gauges --days 35`, all three councils in parallel
(2026-11-21 21:31Z: the first 35-day run, over an empty cache for ECan and for WCRC's raw series;
22:05Z: the next run, which for WCRC and TDC fetches only the newest 48 h):

| council | kind | sites listed | active | first 35-day run | next run (newest 48 h) | usable, 35 d | usable, 30 d | gauges with >= 90 % of 30 d |
|---|---|---|---|---|---|---|---|---|
| WCRC | Hilltop | 69 | 60 | 372 requests, 12.0 MB, 8.3 min, 0 retries | 181 requests, 0.9 MB, 3.5 min (9 inactive: no request) | 97.0 % | 96.7 % | 56 |
| TDC | Hilltop | 179 | 81 | 660 requests, 14.0 MB, 18.7 min, 36 retries | 247 requests, 3.1 MB, 6.3 min, 10 retries (98 inactive: no request) | 99.9 % | 99.9 % | 81 |
| ECan | portal | 110 | 110 | 111 requests, 2.6 MB, 2.3 min, 0 retries | 111 requests, 0.6 MB, 1.9 min (`1_Week` per site; run on a copy of the cache) | 88.1 % | 99.7 % | 110 |
| all | | 358 | 251 | | | 94.0 % | | 247 |

Sites listed = in the site lists; active = record reaching within `inactive_days` (30) of now (only
active gauges are in `gauges_hourly`); usable = share of the active gauges' hours with a value (good
or raw) over the 35 days (841 h) and the last 30 days. ECan's 35-day share is lower because its portal
reaches back a month at most (below). No request failed in either run. The four gauges below 90 % are
WCRC's, with real gaps: Grey Rv @ Waipuna WCRC (server `<Gap/>`s since 2026-10-19), Haast Rv @ Roaring
Billy WCRC and Wanganui Rv @ Smyth Hut (records ended 2026-11-02 and 11-18), Grey Rv @ WCRC Office2
(89.9 %). A routine run costs one MeasurementList, one quality series and one GetData per active
Hilltop gauge, and the site list plus one request per active ECan gauge; known-inactive Hilltop sites
cost nothing until `recheck_days` (7) after their last check.

**Regions.** Canterbury 111 gauges (110 ECan, 1 WCRC), Tasman 80 (TDC and WCRC gauges in the Buller
headwaters), West Coast 57, Marlborough 2 (TDC: Pelorus, Motueka at Red Hills), Otago 1 (WCRC: Young
North Branch, Makarora). **Duplicates:** WCRC and TDC publish the same 7 gauges (Aorere at Perry
Saddle, Cobb, Karamea, Mokihinui, Wangapeka, Maruia, Matakitaki; e.g. `wcrc:Karamea Rv @ Garibaldi`
and `tdc:HY Karamea at Garibaldi`: identical hourly values, r = 1.00), and WCRC's `Arthurs Pass EWS` is
ECan's `Arthur's Pass EWS (ESNZ)`. Both copies are in `gauges_hourly`; `rain scale` counts each such
station once (rf-t26.13, `docs/rainfall-scaling.md`: copies found from the series, one kept, the reason
recorded).

What the first, blind version got wrong, and what changed:

- **WCRC: 0 active of 54.** Each WCRC site keeps two rain series: `Rainfall`, verified up to the last
  audit (e.g. Arthurs Pass EWS 2021-09-28 .. 2023-10-16), and `Rainfall (raw)`, the live telemetry (to
  2026-11-22T07:00 NZST, two hours before the request). The code took `Rainfall`. Now every hinted
  measurement's site list is read and merged (68 sites list `Rainfall (raw)`, 54 `Rainfall`, 18
  `Rainfall (corrected)`; 69 in all), and per site the series whose record reaches nearest to now is
  used (`hilltop.choose_source`; series within 6 h of each other go to the earlier name in
  `measurements`, verified first). A series named raw is never `good` (WCRC sends QC 200 on it anyway).
  A change of series drops the site's cached hours.
- **Request names.** `Measurement=` takes the series' `RequestAs` (`Rainfall (raw)`); `Rainfall [Rainfall
  (raw)]` is refused ("Rainfall is not a measurement in the Rainfall (raw) data source"). Spaces must be
  `%20`: WCRC reads `+` literally ("No data source for Rainfall+(raw)."). The client always sent `%20`
  (`urllib.parse.quote`; test `test_real_wcrc_getdata_and_request_encoding`).
- **Closed sites.** 98 of TDC's 179 sites and 9 of WCRC's 69 have records that ended long ago
  (TDC: 1951 .. 2026-08-19; WCRC: 1992 .. 2026-07-09). They are now inactive: not in `gauges_hourly`, no
  data request, and no request at all on later runs until a recheck. TDC's `Rainfall` is live (verified
  to the last visit, QC 200 after it); `Raw Rainfall` ends at the last visit; 9 sites list only
  `Rainfall [Combined Data]` (a quality series is not served for those: the records' own `Q1` is used).
- **TDC resets connections** (`Connection reset by peer`, 4-6 % of requests): retried with backoff
  (`retries = 4`), none lost.
- **ECan's Hilltop server** (`wateruse.ecan.govt.nz`) reset every https connection; plain http does not
  leave the container. ECan now comes from its Open Data Portal (below); the Hilltop entry is kept as
  `[gauges.councils.ecan_hilltop]`, `enabled = false`.

## Time zones

All three councils send NZST (UTC+12 all year); `timezone = "NZST"` for each. Evidence, 2026-11-21:

| council | newest records at the check | read as NZST | as NZDT | other |
|---|---|---|---|---|
| WCRC (21:09Z) | 08:55 local (5-min gauges), 06:00-08:00 (hourly ones) | 14 min old | 74 min | the server's `TimeInterval=P1D/now` at 21:05Z: "No data from 21-Nov-2026 09:05:35 to 22-Nov-2026 09:05:35": its clock is on NZST |
| TDC (21:08Z) | `Rainfall` to 09:00, telemetry log (`Comms`) to 09:04:36 | 8 min, 4 min | 68 min, 64 min | TDC keeps a separate `Rainfall NZDT` copy of its rainfall |
| ECan (21:22Z) | last samples 09:00-09:15 at 97 of 110 sites | 7-22 min | 67-82 min | ECan: "times are in NZ standard time; during daylight saving add one hour"; the portal's JSON/XML append `+13:00` (the server's own offset, a .NET serialisation of the same wall-clock value): not used, the CSV is |

None was in the future read as NZST (the run warns when a record is more than 10 min ahead of the
clock; a run's own "now" is fixed at its start, so the check uses the wall clock in a live run).

**The same station from two councils.** Converted to UTC, WCRC's `Arthurs Pass EWS` and ECan's
`Arthur's Pass EWS (ESNZ)` agree hour for hour (r = 1.00 at lag 0, 0.91 at ±1 h, 621 mm each over 741
h), and so do the 7 gauges that WCRC and TDC both publish (r = 0.99-1.00 at lag 0, equal totals). So the
three conversions agree with each other, and ECan's own statement (NZST) fixes them all.

**ECan's daily columns.** The site list's `-1 Day .. -7 Day` totals are reproduced by the hourly rows
(read as NZST hour ends) for 748 of 767 site-days when the days run from 23:00 to 23:00 NZST, i.e.
midnight to midnight on the NZ wall clock (NZDT in November): 669 with NZST midnights, 632 if a row
held the hour after its stamp. So the hours are NZST hour ends, and the website's days are wall-clock
days.

**Lag check against ECMWF.** Gauge hours against the ECMWF IFS hourly rain (Open-Meteo, the
observed-proxy leads 1-6; 189 hours, 2026-11-14 01Z .. 11-21 21Z; 413 requests, 141 MB, 4 min) at the
gauge's 0.1° cell, for the 136 gauges with >= 80 % usable hours, >= 10 mm and >= 5 mm in the cell.
Pooled Pearson r of gauge hour `h + k` against ECMWF hour `h` (k = 0 if the conversion is right; gauge
times that were really UTC would peak at k = -12, NZDT read as NZST at k = +1):

| lag k (h) | -13 | -12 | -6 | -3 | -2 | -1 | **0** | +1 | +2 | +3 | +6 | +12 | +13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all 136 | 0.15 | 0.18 | 0.47 | 0.70 | 0.77 | 0.83 | **0.85** | 0.85 | 0.82 | 0.78 | 0.59 | 0.27 | 0.23 |
| WCRC own 43 | 0.14 | 0.18 | 0.48 | 0.71 | 0.79 | 0.84 | **0.87** | 0.87 | 0.84 | 0.81 | 0.63 | 0.27 | 0.24 |
| WCRC EWS 5 | 0.19 | 0.20 | 0.53 | 0.71 | 0.79 | 0.86 | **0.87** | 0.83 | 0.78 | 0.73 | 0.48 | 0.19 | 0.16 |
| TDC 28 | -0.03 | -0.03 | 0.07 | 0.39 | 0.56 | 0.73 | **0.84** | 0.84 | 0.72 | 0.55 | 0.19 | -0.01 | -0.02 |
| ECan own 58 | 0.14 | 0.17 | 0.45 | 0.66 | 0.72 | 0.75 | **0.75** | 0.74 | 0.71 | 0.66 | 0.46 | 0.22 | 0.20 |
| ECan ESNZ EWS 2 | 0.19 | 0.22 | 0.61 | 0.83 | 0.89 | 0.91 | **0.92** | 0.88 | 0.85 | 0.80 | 0.64 | 0.37 | 0.32 |

The best lag is 0 for all gauges pooled (0 for 3-hour sums too, r 0.88), and 48 of the 136 gauges
peak at 0 on their own, 98 within ±1 h (median 0). Times read as UTC are ruled out (r 0.18 at -12 h).
ECMWF cannot settle the last hour, though: TDC, WCRC's and ECan's own gauges are within 0.003 between 0
and an hour either side, so the hour comes from the record times above. The gauges whose own best lag
is 3 h or more off are 14 ECan gauges, mostly in the Mid and South Canterbury foothills, with little rain
in the week (11-37 mm against 19-61 mm in the cell: one event, which ECMWF placed differently), and five
wet West Coast gauges at +3 / +4 (Taipo, Tuke, two on the Haast, Young: the front's timing; r at lag 0
is 0.63-0.88 for them).

**Open: Arthur's Pass.** ECan's own `Arthur's Pass` gauge leads the co-located ESNZ EWS by an hour
(r = 0.99 at -1 h, 0.91 at 0; 636 vs 621 mm). The EWS is the same in ECan's and WCRC's copies, so one of
the two series at that site is stamped an hour off at its source; ECMWF puts ECan's own gauges at -1
or 0 (0.748 vs 0.747) and cannot tell. It matters to hourly comparisons, not to 30-day totals.

## Endpoints

| council | kind | server | file / dataset | notes |
|---|---|---|---|---|
| `wcrc` West Coast Regional Council | hilltop | `https://hilltop.wcrc.govt.nz/` (Hilltop Server 2404.2.2.62) | `data.hts` (serves the default file, `websitedata20260424.hts` on 2026-11-21; `PublicLive.hts` answers the same) | series `Rainfall`, `Rainfall (corrected)`, `Rainfall (raw)`; hourly or 5-min incremental records, QC 200 on raw |
| `tdc` Tasman District Council | hilltop | `https://envdata.tasman.govt.nz/` | `data.hts` | series `Rainfall` (live), `Raw Rainfall` (to the last visit); 15-min incremental records; QC 500 to the last visit, 200 after |
| `ecan` Environment Canterbury | ecan-portal | `https://data.ecan.govt.nz/` (Open Data Portal) | `data/51` site list, `data/78` hourly totals | below |
| `ecan_hilltop` (off) | hilltop | `https://wateruse.ecan.govt.nz/` | `data.hts` (unverified) | resets every https connection from here |

Hilltop requests (key-value, documented by Hawke's Bay Regional Council [9] [10], implemented in
hilltop-py 2.4 [4] and HorizonsRC's `whurl` [11]):

```
<root><file>.hts?Service=Hilltop&Request=MeasurementList                       # measurements in the file
<root><file>.hts?Service=Hilltop&Request=SiteList&Location=LatLong&Measurement=Rainfall%20(raw)
<root><file>.hts?Service=Hilltop&Request=MeasurementList&Site=<site>           # per series: From/To, units, RequestAs
<root><file>.hts?Service=Hilltop&Request=GetData&Site=<site>&Measurement=<RequestAs>&From=<t>&To=<t>&ShowQuality=Yes
<root><file>.hts?Service=Hilltop&Request=GetData&...&tsType=StdQualSeries      # the quality-code series
```

`Request=Status` lists the server's data files; there is no request that gives every site's record end
at once (`MeasurementList&Measurement=` without a site lists measurements only), so record ends are
kept in `sites.json` and known-inactive sites are skipped from it.

### ECan Open Data Portal

ECan's portal (ASP.NET) answers `data/<method id>/<collection>/<dataset name>/<format>?<params>`; only
the method id matters (any path words give the same answer), formats CSV, XML, JSON, JSONP. The
dataset pages that document the parameters (`/Catalogue/Method?MethodId=78`) redirect to a click-through
data agreement (a form asking for a user type and "I understand and agree"); it was not accepted. The
parameters were found by probing the data URLs: a wrong parameter name answers HTTP 500, a right name
with a wrong value an empty table (one request per name tried).

```
https://data.ecan.govt.nz/data/51/Rainfall/Rainfall%20summary%20by%20area/CSV?Sites=All     # or North, South
https://data.ecan.govt.nz/data/78/Rainfall/Rainfall%20for%20individual%20site/CSV?SiteNo=231910&Period=2_Days
https://data.ecan.govt.nz/data/134/Rainfall/Rainfall%20summary%20for%20site/CSV?SiteNo=231910  # one row (not used)
```

- **Site list** (`data/51`, `Sites=All`): 110 rows: `Site Name, Last Sample, Last Hour, RainToday, -1 Day ..
  -7 Day, Total Rainfall, SITE_NO, ShortName, Sub_text, SiteOwner, Location (N/S), WGS84_Longitude,
  WGS84_Latitude, OwnerLogo`. Owners: ECan 93 (written `ECan`, `Ecan`, `ECAN`), Christchurch City Council
  11, Earth Sciences New Zealand 6; one site (Tai Tapu Yard) has no location. The last sample per site
  makes inactive sites free.
- **Hourly totals** (`data/78`): `site_no, DateTime, RainfallTotal`, hourly for `Period` = `2_Days`,
  `1_Week`, `2_Weeks`, `1_Month`; `3_Months` and `1_Year` are daily; other values (`1_Day`, `7_Days`,
  `6_Weeks`, `2_Months`, ...) give an empty table, and `StartDate`/`EndDate` are not taken. A period
  counts back from about the site's last whole hour, not from the request: 46-47 rows for `2_Days`,
  167 for `1_Week`, 334 for `2_Weeks`, 742 for `1_Month` (to 22/11). So a month is the longest hourly
  history the portal has; the cache keeps what earlier runs got (`keep_days` 60). Each run asks for the
  shortest period that surely reaches the oldest hour the cache lacks (spans taken as 45, 165, 333 and
  669 h), and uses the answer as far back as its rows go; hours older than a month are no reason for a
  request. Stamps are the end of the hour, a few ms either side (`9:59:59 AM`, `12:00:00 PM`; JSON
  `09:59:59.943`): rounded to the nearest hour. The newest row is the hour ending at the last sample
  when that is on the hour (a part hour is not given until it is complete). Checked: the 21/11 hours of
  Shingle Fan sum to the site list's `-1 Day` (17.5 mm), and the day columns as a whole (above).
- **Dry hours have no row.** Most ECan gauges record events (0.5 mm tips) and an hour without a record
  has no row: North Esk has a row every 4 hours (zero heartbeats) when dry, Mount Byrne only rows while
  it rains (338 mm in 34 h on 25-26 October, then nothing for days). An hour in the period and before
  the last sample without a row is therefore 0 mm, as for Hilltop records; it cannot be told apart
  from an outage (the portal marks none), so a long dry spell at an event gauge reads as dry. The
  gauge scaling leaves out dry spells while the neighbours and ECMWF are wet (rf-t26.16,
  `docs/rainfall-scaling.md`, "Likely outages"); the cache keeps them as they came.
- No quality codes, and the data terms call the values provisional: every ECan hour is `raw`.

### Hosts from the development container

Reachable over https: `hilltop.wcrc.govt.nz`, `envdata.tasman.govt.nz`, `data.ecan.govt.nz`.
`wateruse.ecan.govt.nz` resets every https connection. Refused by the proxy (HTTP 403):
`www.ecan.govt.nz` (ECan's rainfall pages), `gis.ecan.govt.nz` (the Canterbury Maps layer of rainfall
sites). Plain `http://` does not leave the container at all; the `http://` fallbacks in the config are
for other machines.

## Licences and attribution

| council | licence | attribution |
|---|---|---|
| WCRC | not stated for the Hilltop service; WCRC calls its web rainfall and river data raw (straight from telemetry, not quality controlled) and asks for data requests to hydrologydata@wcrc.govt.nz [2] | "Rainfall data: West Coast Regional Council" |
| ECan | the Open Data Portal: "All data unless specifically stated is licensed under a Creative Commons Attribution 4.0 International License" [6]. Its data terms (`/Catalogue/Agreement`) add: Environment Canterbury is to be named as the source, without ECan, Metro or Metroinfo logos; no advertising in applications using the data without ECan's permission; data shown on a public site is to be kept current or removed; the user indemnifies ECan for losses from a breach; values are provisional, not reviewed. CCC and ESNZ gauges are served under the same page (owner per gauge in `sites.json`) | "Rainfall data: Environment Canterbury (with Christchurch City Council and Earth Sciences New Zealand gauges)" |
| TDC | not stated for the Hilltop service: check TDC's terms before publishing anything derived | "Rainfall data: Tasman District Council" |

Each council's `licence` and `attribution` are in the config and copied into `gauges_hourly.json`
(`councils.<id>`) and the `.npz` meta. Confirm WCRC's and TDC's terms before gauge data or scaled rain
derived from it is published on the site (rf-t26.14): until then `[scaling] publish = false` keeps the
published rain files raw (`docs/rainfall-scaling.md`).

## Running

```bash
uv run river-finder rain gauges                         # every enabled council: the last 35 days on the first
                                                        # run, then only new hours + the newest 48 h
uv run river-finder rain gauges --council wcrc tdc --days 7
uv run river-finder rain gauges --sites-only            # site lists and placement only (sites.json)
uv run river-finder rain gauges --discover              # probe the enabled councils' servers (discover.json)
uv run river-finder rain gauges --discover --council ecan_hilltop   # ... or a disabled one
uv run river-finder rain gauges --refetch-hours 0       # do not fetch cached hours again
uv run river-finder rain gauges --data /path/to/data/nz # another national data directory
uv run river-finder rain status                         # includes a line on the gauge cache
```

Councils run in parallel, each server one request at a time with at least `request_gap_s` (1 s)
between requests; HTTP 429/5xx and network errors are retried `retries` times with doubling backoff.
Hilltop, per run and active site: one MeasurementList, one quality series, and one GetData per
`chunk_days` (10) of hours to fetch. ECan: the site list and one request per active site. Exit status
75 when no council's server answered (as `rain ecmwf`: try later); the cache and the last outputs are
kept. A council that fails keeps its cached gauges in the output, their new hours `not_fetched`.

## Output (`data/nz/rain/gauges/`)

- `sites.json`: per council the server root, file and measurements found, when, how many sites and
  active ones; per gauge `id` (`"<council>:<site name>"`), `council`, `name`, `measurement` (the name
  GetData asks for), `datasource` (the series chosen), `raw` (raw telemetry), `site_no` and `owner`
  (ECan), `lon`/`lat` (WGS84; the server's NZGD2000), `x`/`y` (NZTM2000, EPSG:2193), `cell` (flat
  index `row * nx + col` on `[grids.ecmwf]`, -1 outside or unplaced), `cell_row`, `cell_col`, `region`
  (network region name), `region_id` (site id, `"west-coast"`), `region_method`, `units`,
  `interpolation`, `record_first`/`record_last` (UTC; ECan: the last sample), `checked` (when the record
  times were read), `active`;
- `series/<council>/<site-slug>-<hash>.npz`: one gauge's cached hours (contiguous; `mm`, `qc`,
  `state`, `flags`, kept `keep_days` = 60) and its recent fetches;
- `gauges_hourly.npz`: the active gauges only: `hour` (T,) consecutive hours ending (hours since the
  epoch, as the rain buffers), `mm` float32 (G, T) with NaN = missing, `qc` int16 (-1 none), `state` and
  `flags` uint8; per gauge `id`, `council`, `name`, `lon`, `lat`, `x`, `y`, `cell`, `cell_row`,
  `cell_col`, `region_of` (index into `meta.regions`, -1 none); `meta` (grid, regions and ids,
  councils with licence and attribution, state and flag names). `GaugeHours.load` reads it;
- `gauges_hourly.json`: the run report: per council (kind, server, file, measurements, status, sites,
  active sites, skipped inactive sites, gauges in the output, usable share over the output and the last
  30 days, gauges with >= 90 % of the last 30 days, errors, warnings, time-zone counts, requests and
  bytes, ECan's owners), per region (gauges, how many cover >= 90 % of the last 30 days), per gauge
  (place, cell centre, region, record times, hours per state over the cache and the last 30 days, mm in
  the last 24 h and 30 days, last hour with a value);
- `discover.json` (`--discover` only).

**Hours.** Hour `h` holds the rain in `(h - 1, h]` UTC (hour ending): a record stamped `hh:00:00`
belongs to the hour ending `hh:00`. `method = "raw"` (default for Hilltop) sums the records in each
hour (incremental tips or interval totals); an hour inside the record with no record is 0 mm.
`total-end` / `total-start` take the server's own hourly totals instead (whether a Hilltop server
stamps them at the start or end of the hour is not documented: check before switching). ECan's
portal totals are end-stamped and summed like records: an hour without a row, inside the period and
before the last sample, is 0 mm.

**States** (use `state <= 1`, `GaugeHours.ok`): 0 good (value, quality code >= 500 for the whole hour,
not a raw series), 1 raw (value, not quality coded yet: no code, QC 0 or 200, a raw series, or ECan),
2 suspect (value kept, not to be used), 3 missing (NaN). **Flags** (bits): 1 gap (a `<Gap/>` covers the
hour), 2 partial (part of the hour in a gap or outside the record), 4 outside (before the record's
first or after its last time), 8 no_value (`total-*`: no total returned), 16 range (a record
unreadable or negative, or the hour above `max_mm_per_hour` = 200), 32 low_qc (QC 300-499 or not a NEMS
code), 64 qc_missing (QC 100), 128 not_fetched. Quality codes follow the NEMS National Quality Code
Schema (600 good, 500 fair, 400 compromised, 200 not verified, 100 missing record) [12]; a code in the
quality series ends the period it applies to (TDC's HY Takaka at Harwoods: 500 at 2026-10-28T06:30,
the last visit, then 200 at 06:30:01 and at the record's end, 2026-11-22T09:00).

**Dry spells.** A tipping-bucket gauge that sends only tips records nothing when it does not rain, so
the hours after its last record cannot be told apart from an outage: they are `outside` (missing)
until the gauge reports again; `refetch_hours` fetches them again. The WCRC and TDC series looked at
send a record every interval, zeros included (WCRC hourly or 5-min, TDC 15-min), so for them the record
end is the telemetry's last poll.

**Time zones.** `timezone = "NZST"` (UTC+12 all year) for all three, checked above.
`Pacific/Auckland` reads NZ wall-clock time (NZDT, UTC+13, from 02:00 NZST on the last Sunday of
September to 03:00 NZDT on the first Sunday of April): records in the repeated hour in April are placed
in time order (a time that goes back starts the NZST pass), times in the skipped hour in September are
read as NZST. `UTC` for a server that sends UTC. Times with an offset (`Z`, `+12:00`) are read as
given. Requests (`From`/`To`) are sent in the same zone, with an hour of margin (two for the wall
clock) that is cut off again.

**Regions** match the reach network's (`buffer.region_of`, the river-guide regions of
`national.region_filters`, the site's region ids): point in polygon when `councils.gpkg` and
`large_catchments.gpkg` are in the data directory (as the segment table's `region`), else the region
with the most catchment area in the gauge's ECMWF cell (`weights_ecmwf.npz`), else the council's
`region`. A WCRC gauge in the upper Buller is in Tasman, an ECan gauge at Arthur's Pass in Canterbury.

For rf-t26.5 (done: `river-finder rain scale`, `src/river_finder/rainfall/scaling.py`,
`docs/rainfall-scaling.md`), the gauge side in short:

```python
from river_finder.rainfall.config import load_config
from river_finder.rainfall.gauges import GaugeHours

cfg = load_config()
g = GaugeHours.load(cfg.gauges_dir / "gauges_hourly.npz")
w = g.window(hours=720)                      # per gauge: mm over usable hours, n_ok, n_suspect, n_missing
for region, rows in g.region_rows().items():
    use = rows[w["n_ok"][rows] >= 0.9 * 720]  # gauges with 90 % of the 30 days
    cells = g.cell[use]                       # their ECMWF cells, to compare with the grid's hours there
```

## Sources

1. Ministry for the Environment, *Stocktake of data quality code and metadata usage in regional council
   water quality datasets*, https://environment.govt.nz/assets/publications/Freshwater/stocktake-of-data-quality-code-and-metadata-usage-in-regional-council-water-quality-datasets.pdf
2. West Coast Regional Council, River levels & rainfall, https://www.wcrc.govt.nz/environment/water/river-levels-rainfall ;
   flood watch, https://flood-watch.wcrc.govt.nz/
3. http://wateruse.ecan.govt.nz/RiverRecorderQcEcanDaily.hts (indexed as "Hilltop Server")
4. hilltop-py 2.4 (M. Kittridge, Apache-2.0), https://github.com/mullenkamp/hilltop-py (`hilltoppy/utils.py`
   `build_url`, `web_service.py`, `mountain_top.py`; tests), read from the PyPI sdist
5. Tasman District Council, Rainfall report, https://www.tasman.govt.nz/my-region/environment/environmental-data/rainfall/report
6. ECan Open Data Portal, https://data.ecan.govt.nz/ : Rainfall collection, `/Catalogue/Search?CollectionId=9`
   (datasets 78 "Rainfall for individual site", 51 "Rainfall summary by area", 134 "Rainfall summary for site");
   data terms, `/Catalogue/Agreement?...&AgreementFile=Agreement.htm&AgreementRequirements=General` (read 2026-11-21)
7. Environment Canterbury Developer Data Portal, https://apidevelopers.ecan.govt.nz/ (terms: /terms)
8. Environment Canterbury, Rainfall data, https://www.ecan.govt.nz/data/rainfall-data (via a search engine's
   summary: "times are in NZ standard time"; site pages `/data/rainfall-data/sitedetails/<SITE_NO>`)
9. Hawke's Bay Regional Council, *Hilltop Server (Web Service)* (2016), https://www.hbrc.govt.nz/assets/Document-Library/Council-Data/20170426-HilltopServerTrimmed.pdf
10. Hawke's Bay Regional Council, Time Series Data, https://www.hbrc.govt.nz/our-council/open-data/time-series-data/
11. Horizons Regional Council, whurl (Hilltop Server URL generator), https://github.com/HorizonsRC/whurl
12. NEMS, National Quality Code Schema v3.0.1, https://www.nems.org.nz/
13. `data/78` with `SiteNo` and `Period` as used by public scripts, e.g. https://github.com/MatthewDWilson/rainapp
    (`R/datahandling.R`) and https://github.com/chrisb2/water-system (`weather.py`, `Period=2_Days`)

# Rain gauges (rf-t26.4)

Hourly rain-gauge totals from the regional councils' Hilltop servers, cached locally, for the gauge
scale factors of rf-t26.5 (grid totals vs gauge totals per source and region over a rolling 30 days).
First councils: West Coast (WCRC), Canterbury (ECan), Tasman (TDC).

Code: `src/river_finder/rainfall/hilltop.py` (client, XML, time zones) and
`src/river_finder/rainfall/gauges.py` (hours, quality states, placement, cache, outputs; its docstring is
the reference). Config: `[gauges]` in `config/rainfall.toml`. Tests: `tests/test_rainfall_gauges.py`
with synthetic XML in `tests/fixtures/hilltop/`.

## Status: not verified against a live server

The development container's network proxy refused every council host (HTTP 403 on CONNECT; see the
list below), and so was fetching documentation from council sites (`www.hbrc.govt.nz`). So the client is built
against the documented Hilltop XML format and the fixtures are hand-written from it. Before relying
on it, run the discovery where the servers answer (any normal machine, a GitHub Actions runner):

```bash
uv run river-finder rain gauges --discover            # every council; or --council wcrc
cat data/nz/rain/gauges/discover.json                 # per server root x .hts file: measurements,
                                                      # collections, data files, sites, a sample
```

and fix `hts`, `measurements` and `timezone` per council from what it prints. Things to check:

- **The `.hts` file with rainfall.** Only the hosts are documented; `data.hts` (Hilltop Server's
  usual name) is a guess for all three. `--discover` also prints `Request=Status` (the server's data
  files) and the collections, which name the right file when `data.hts` is wrong.
- **The time zone.** `discover.json` gives, for a sample site, how far its newest record is from now
  read as NZST, NZ wall clock and UTC: a live gauge's newest record is minutes old in the right zone,
  and an hour "in the future" read as NZST means the server is on NZDT. The fetch run warns about the
  same thing per site.
- **Quality codes.** Whether the server sends `Q1` with `ShowQuality=Yes` and has a `StdQualSeries`
  (both optional here).

## Endpoints

| council | server roots tried (in order) | file | where the host is documented |
|---|---|---|---|
| `wcrc` West Coast Regional Council | `https://hilltop.wcrc.govt.nz/`, `http://hilltop.wcrc.govt.nz/` | `data.hts` (unverified) | MfE, *Stocktake of data quality code and metadata usage in regional council water quality datasets* (2023), table of council data servers: `http://hilltop.wcrc.govt.nz/wq.hts?service=Hilltop&request=GetData...` (water quality) [1]; WCRC's flood-watch site reads a Hilltop server ("Failed to retrieve Hilltop server data" on `flood-watch.wcrc.govt.nz`) [2] |
| `ecan` Environment Canterbury | `https://wateruse.ecan.govt.nz/`, `http://wateruse.ecan.govt.nz/` | `data.hts` (unverified) | a public Hilltop Server page at `http://wateruse.ecan.govt.nz/RiverRecorderQcEcanDaily.hts` (search index) [3]; the MfE stocktake lists `wateruse.ecan.govt.nz/wqlawa.hts` [1]; hilltop-py's tests name `http://data.ecan.govt.nz/` + `WQAll.hts` [4] |
| `tdc` Tasman District Council | `https://envdata.tasman.govt.nz/`, `http://envdata.tasman.govt.nz/` | `data.hts` (unverified) | the MfE stocktake lists `envdata.tasman.govt.nz/WaterQuality.hts` [1]; TDC's rainfall pages cover Tasman and Nelson sites [5] |

The MfE stocktake was read through a search engine's summary of it (the PDF itself could not be
fetched here). ECan's own public route for rainfall is not Hilltop: its **Open Data Portal**
(`data.ecan.govt.nz`, collection "Rainfall": "Rainfall for individual site", "Rainfall summary for
site", "Rainfall summary by area"; csv, json, xml) [6], and a newer **Environmental Observations
API** on `apidevelopers.ecan.govt.nz` (sign-in and a subscription key; river flow so far) [7]. ECan
says most of its 90+ rainfall sites record events (0.5 mm tips) and that its times are NZ standard
time [8]. Neither is implemented: their URL templates could not be read here. If ECan's Hilltop
server has no public rainfall file, an adapter for the Open Data Portal is the next step (same
`HourBlock` output: records -> `hours_from_records`).

Request format (all councils): Hilltop Server key-value requests, documented by Hawke's Bay Regional
Council [9] [10] and implemented in hilltop-py 2.4 [4] (and HorizonsRC's `whurl` [11]):

```
<root><file>.hts?Service=Hilltop&Request=MeasurementList                       # measurements in the file
<root><file>.hts?Service=Hilltop&Request=SiteList&Location=LatLong&Measurement=Rainfall
<root><file>.hts?Service=Hilltop&Request=MeasurementList&Site=<site>           # From/To, units, RequestAs
<root><file>.hts?Service=Hilltop&Request=GetData&Site=<site>&Measurement=<m>&From=<t>&To=<t>&ShowQuality=Yes
<root><file>.hts?Service=Hilltop&Request=GetData&...&tsType=StdQualSeries      # the quality-code series
   (method total-*: &Method=Total&Interval=1%20hour&Alignment=00:00)
```

Measurement names differ between councils: `measurements = ["Rainfall", "Rainfall Total", "Rain"]` is
tried in order against the file's MeasurementList, case-insensitively, and `"Rainfall"` also matches
`"Rainfall [Rainfall]"` (name plus data source). Per site, the name sent with GetData is the data
source's `RequestAs` when the server gives one.

### Hosts refused in the development container

All with `CONNECT tunnel failed, response 403` ("no rule or network setting allows this host"):
`hilltop.wcrc.govt.nz`, `envirodata.wcrc.govt.nz`, `www.wcrc.govt.nz`, `wateruse.ecan.govt.nz`,
`data.ecan.govt.nz`, `www.ecan.govt.nz`, `apidevelopers.ecan.govt.nz`, `envdata.tasman.govt.nz`,
`www.tasman.govt.nz`, `www.hbrc.govt.nz` (documentation). Plain `http://` does not leave the container
at all (no HTTP proxy). The client does not retry a proxy refusal (`fetch.Http`).

## Licences and attribution

| council | licence | attribution |
|---|---|---|
| WCRC | not stated for the Hilltop service; WCRC calls its web rainfall and river data raw (straight from telemetry, not quality controlled) and asks for data requests to hydrologydata@wcrc.govt.nz [2] | "Rainfall data: West Coast Regional Council" |
| ECan | Canterbury Maps open data is Creative Commons Attribution; terms of the Hilltop service and the Open Data Portal not checked here; the developer APIs have their own terms of use [7] | "Rainfall data: Environment Canterbury" |
| TDC | not stated for the Hilltop service: check TDC's terms before publishing anything derived | "Rainfall data: Tasman District Council" |

Each council's `licence` and `attribution` are in the config and copied into `gauges_hourly.json`
(`councils.<id>`) and the `.npz` meta. Confirm the terms before gauge data or scaled rain derived from
it is published on the site.

## Running

```bash
uv run river-finder rain gauges                         # every enabled council: the last 35 days on the first
                                                        # run, then only new hours + the newest 48 h
uv run river-finder rain gauges --council wcrc tdc --days 7
uv run river-finder rain gauges --sites-only            # site list and placement only (sites.json)
uv run river-finder rain gauges --discover              # probe servers and files (discover.json)
uv run river-finder rain gauges --refetch-hours 0       # do not fetch cached hours again
uv run river-finder rain status                         # includes a line on the gauge cache
```

Councils run in parallel, each server one request at a time with at least `request_gap_s` (1 s)
between requests; HTTP 429/5xx and network errors are retried `retries` times with doubling backoff.
Per run and site: one MeasurementList, one quality series, and one GetData per `chunk_days` (10) of
hours to fetch; a site whose record ended before the hours asked (closed sites stay in site lists) costs
no data request. Exit status 75 when no council's server answered (as `rain ecmwf`: try later); the
cache and the last outputs are kept. A council that fails keeps its cached gauges in the output, their
new hours `not_fetched`.

## Output (`data/nz/rain/gauges/`)

- `sites.json`: per council the server root, file and measurement found and when; per gauge `id`
  (`"<council>:<site name>"`), `council`, `name`, `measurement`, `lon`/`lat` (WGS84; the server's
  NZGD2000), `x`/`y` (NZTM2000, EPSG:2193), `cell` (flat index `row * nx + col` on `[grids.ecmwf]`, -1
  outside or unplaced), `cell_row`, `cell_col`, `region` (network region name), `region_id` (site id,
  `"west-coast"`), `region_method`, `units`, `interpolation`, `record_first`/`record_last` (UTC);
- `series/<council>/<site-slug>-<hash>.npz`: one gauge's cached hours (contiguous; `mm`, `qc`,
  `state`, `flags`, kept `keep_days` = 60) and its recent fetches;
- `gauges_hourly.npz`: `hour` (T,) consecutive hours ending (hours since the epoch, as the rain
  buffers), `mm` float32 (G, T) with NaN = missing, `qc` int16 (-1 none), `state` and `flags` uint8;
  per gauge `id`, `council`, `name`, `lon`, `lat`, `x`, `y`, `cell`, `cell_row`, `cell_col`,
  `region_of` (index into `meta.regions`, -1 none); `meta` (grid, regions and ids, councils with
  licence and attribution, state and flag names). `GaugeHours.load` reads it;
- `gauges_hourly.json`: the run report: per council (server, file, measurement, status, errors,
  warnings, time-zone counts, requests and bytes), per region (gauges, how many cover >= 90 % of the
  last 30 days), per gauge (place, cell centre, region, record times, hours per state over the cache and
  the last 30 days, mm in the last 24 h and 30 days, last hour with a value);
- `discover.json` (`--discover` only).

**Hours.** Hour `h` holds the rain in `(h - 1, h]` UTC (hour ending): a record stamped `hh:00:00`
belongs to the hour ending `hh:00`. `method = "raw"` (default) sums the records in each hour
(incremental tips or interval totals); an hour inside the record with no record is 0 mm.
`total-end` / `total-start` take the server's own hourly totals instead (whether a server stamps them
at the start or end of the hour is not documented: check before switching).

**States** (use `state <= 1`, `GaugeHours.ok`): 0 good (value, quality code >= 500 for the whole hour),
1 raw (value, not quality coded yet: no code, QC 0 or 200, the norm for recent telemetry), 2 suspect
(value kept, not to be used), 3 missing (NaN). **Flags** (bits): 1 gap (a `<Gap/>` covers the hour),
2 partial (part of the hour in a gap or outside the record), 4 outside (before the record's first or
after its last time), 8 no_value (`total-*`: no total returned), 16 range (a record unreadable or
negative, or the hour above `max_mm_per_hour` = 200), 32 low_qc (QC 300-499 or not a NEMS code),
64 qc_missing (QC 100), 128 not_fetched. Quality codes follow the NEMS National Quality Code Schema
(600 good, 500 fair, 400 compromised, 100 missing record) [12]; a code in the quality series is taken
to end the period it applies to (time after the last code is not coded yet): this convention is an
assumption to check with `--discover`.

**Dry spells.** A tipping-bucket gauge records nothing when it does not rain, so the hours after its
last record cannot be told apart from an outage: they are `outside` (missing) until the gauge reports
again; `refetch_hours` fetches them again. Over a 30-day comparison this only drops dry hours.

**Time zones.** Hilltop sends times without a zone. `timezone = "NZST"` (UTC+12 all year, what NEMS
asks of recorders and what ECan says it uses) is the default for all three councils; `Pacific/Auckland`
reads NZ wall-clock time (NZDT, UTC+13, from 02:00 NZST on the last Sunday of September to 03:00 NZDT on
the first Sunday of April): records in the repeated hour in April are placed in time order (a time that
goes back starts the NZST pass), times in the skipped hour in September are read as NZST. `UTC` for a
server that sends UTC. Times with an offset (`Z`, `+12:00`) are read as given. Requests (`From`/`To`)
are sent in the same zone, with an hour of margin (two for the wall clock) that is cut off again.

**Regions** match the reach network's (`buffer.region_of`, the river-guide regions of
`national.region_filters`, the site's region ids): point in polygon when `councils.gpkg` and
`large_catchments.gpkg` are in the data directory (as the segment table's `region`), else the region
with the most catchment area in the gauge's ECMWF cell (`weights_ecmwf.npz`), else the council's
`region`. A WCRC gauge in the upper Buller is in Tasman, an ECan gauge at Arthur's Pass in Canterbury.

For rf-t26.5:

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
6. ECan Open Data Portal, Rainfall collection, https://data.ecan.govt.nz/Catalogue/Search?CollectionId=9
7. Environment Canterbury Developer Data Portal, https://apidevelopers.ecan.govt.nz/ (terms: /terms)
8. Environment Canterbury, Rainfall data, https://www.ecan.govt.nz/data/rainfall-data
9. Hawke's Bay Regional Council, *Hilltop Server (Web Service)* (2016), https://www.hbrc.govt.nz/assets/Document-Library/Council-Data/20170426-HilltopServerTrimmed.pdf
10. Hawke's Bay Regional Council, Time Series Data, https://www.hbrc.govt.nz/our-council/open-data/time-series-data/
11. Horizons Regional Council, whurl (Hilltop Server URL generator), https://github.com/HorizonsRC/whurl
12. NEMS, National Quality Code Schema v3.0.1, https://www.nems.org.nz/

# Live rain: the hourly job

`.github/workflows/rain.yml` (bead rf-t26.9) publishes upstream rain for every reach the site draws,
every hour, with no server. The map layer that reads it is rf-t26.10.

## How it works

Each run, on GitHub Actions at :23 past the hour (scheduled runs can start late) or from the Actions tab:

1. checks out the code and, into `rain-data/`, the **`rain-inputs`** branch (~35 MB);
2. `uv sync --frozen --no-dev`: runtime dependencies only (~300 MB of wheels, ~15 s cold);
3. `river-finder rain ecmwf --data rain-data --no-cache --retries 7`: the observed window, the 24 h
   up to the latest whole hour ("now"), and the next 24 h from the hour after it, from Open-Meteo's
   hourly copy of ECMWF IFS (~36 MB, ~1 min). Each observed hour comes from the newest IFS run that
   has it: leads 1-6 of the runs before the latest, then the latest run's own leads up to now (5-11 h
   after its base time; `[ecmwf] obs_leads = [1, 18]` in `config/rainfall.toml`);
4. `river-finder rain totals --data rain-data`: upstream mm per reach and window (~8 s);
5. `river-finder rain publish --data rain-data --out $RUNNER_TEMP/rain-out`: `index.json` plus one
   `<region>.bin.gz` per national region (~4 s);
6. `.github/scripts/rain-branch.sh push`: one orphan commit (author github-actions[bot], message =
   the run times and the forecast run's age), force-pushed to the **`rain`** branch. The branch only
   ever holds the latest files.

The job is stateless: step 3 rebuilds both 24 h buffers from scratch each time, so nothing is
carried between runs. Measured on 2026-11-20 in a fresh directory: 24/24 observed and 24/24
forecast hours, 35.7 MB in 101 requests, 57 s; totals 7.6 s; publish 4.0 s.

Failures email the owner, so the job fails only on real errors:

| `rain ecmwf` | meaning | job |
|---|---|---|
| exit 0 | data fetched | totals, publish, push |
| exit 75 | no run published yet, or the bucket did not answer after its retries (HTTP 429/5xx are retried with doubling backoff) | warning; the published files stay. Fails once they are `STALE_HOURS` (12) old |
| exit 124 | the fetch took over 12 min | same as 75 |
| anything else | a bug or a changed data format | fails |

When no newer ECMWF run is out yet, the job still publishes: same runs, honest times
(`index.json` gives every run's age). The observed window ends at "now" unless no run has published
the newest hours within 18 h of its base time (ECMWF or Open-Meteo hours late); it then ends at the
newest hour one has, and `observed.hours_behind_now` says by how much. `scaled` in `index.json` is
true only once gauge scale factors (rf-t26.5, `docs/rainfall-scaling.md`) are applied and may be
published; until then it is false and every reach's scale bits read 1 (raw). This job has no factor
file (`rain scale` runs locally), and `[scaling] publish = false` (rf-t26.14: gauge licences) keeps
published values raw anyway; `index.json` `scaling` says which. "Gauge scaling in the job" below is
the design for running it here.

## Gauge scaling in the job (design, not built)

`rain scale` (`docs/rainfall-scaling.md`) runs locally for now. The hourly job is stateless, and
scaling needs state that grows from day to day:

- the gauge cache `rain/gauges/` (`sites.json` and `series/`: ~0.8 MB; `gauges_hourly.npz` + `.json`:
  ~0.3 MB);
- the cell store `rain/scaling/ecmwf_cells.npz` (ECMWF at the 189 gauge cells: 92 kB for 30 days);
- the factor file `rain/scaling/factors_ecmwf.json` (72 kB) and its append-only history
  (`.history.jsonl`, ~13 kB per new version).

The proposal is a separate daily job and a small extra step in the hourly one. Do not edit `rain.yml`
for this from a branch: a push of the workflow file can unregister the schedule (see the 60-day rule
below; the schedule registers from `main`).

1. **A daily workflow** (for example `rain-scale.yml`, once a day at an off-peak minute). It checks
   out `rain-inputs` (the weights and `scaling_zones.npz` give the zones) and a **`rain-scaling`**
   branch (the gauge cache and `rain/scaling/`) into one data directory. Then it runs `rain gauges --data DIR` (incremental:
   ~6 min wall clock and ~4.6 MB from the three councils, which run in parallel; ~19 min on an empty
   cache) and `rain scale --data DIR`. A day's ~24 new hours from Open-Meteo cost ~18 MB and ~25 s.
   The first backfill of an empty store costs ~380 MB and ~10 min (8 days from Open-Meteo, the rest
   from ECMWF's GRIB files). Last, it force-pushes `rain-scaling` as one orphan commit, as the hourly
   job does with `rain` (~1.5 MB; the history file keeps the record of every version, so git history is
   not needed). A failed council keeps its cached gauges, and their new hours read `not_fetched`.
   The guard rails then hold the factors (gauges with less than 80 % of the hours drop out, and a
   region with fewer than 3 gauges goes back to factor 1).
2. **The hourly job** gets one step before `rain ecmwf`: download `rain/scaling/factors_ecmwf.json`
   from the `rain-scaling` branch into `rain-data/rain/scaling/` (raw.githubusercontent.com, ~0.1 MB,
   under a second; a failed download leaves no file, which means factor 1, version 0). `rain ecmwf`
   then scales the buffers, and `rain totals` sums scaled values. While `[scaling] publish = false`,
   `rain publish` sums the raw values again (+~8 s: it loads the network and accumulates a second
   time), so until rf-t26.14 is settled the step can simply be left out and the job stays as it is.
3. **To check first**, with a `workflow_dispatch` run: do the council servers answer GitHub's runners?
   TDC resets 4-6 % of the connections from the development container, and ECan's Hilltop server
   resets all of them, so ECan comes from its portal. The 60-day rule applies to the daily workflow
   too.

## Where the files are

- Inputs, branch `rain-inputs`: `manifest.json` (files, sizes, sha256, the national build: segment
  table time, weights and network build times, viewer index time; config values; code commit) and
  `rain/weights_ecmwf.npz` (+ `.json`), `rain/network.npz`, `rain/scaling_zones.npz` (+ `.json`: the
  scaling zone of every catchment, rf-t26.16, `docs/rainfall-scaling.md` "Zones"), `rain/viewer.json`
  (site region ids -> network regions with their reach counts). Laid out like a data directory, so
  `--data` points at it. Inputs without the zone file (built before rf-t26.16) still work: the job
  then keeps its buffers per region and says so; it has no factors yet, so nothing changes.
- Outputs, branch `rain`: `index.json` and `<region>.bin.gz`, read by the site from
  `https://raw.githubusercontent.com/Sean-McConnachie/river-finder/rain/index.json` (CORS `*`,
  cached 5 min). About 14 MB plain, 2.1 MB gzipped in all; Canterbury, the largest, 2.5 MB plain,
  0.47 MB gzipped (dry day; wet days compress less).
- The format (the contract for rf-t26.10) is in the docstring of `src/river_finder/rainfall/live.py`;
  `decode_region` there is the reference decoder. Per reach in viewer order: upstream mm for the past
  and next 1/3/6/12/24 h (0.1 mm), upstream area (m³ = mm × area_km2 × 1000), network flags,
  missing hours, products and scaling per window. A region file whose `n` differs from the site's
  reach count belongs to another national build and must not be drawn.
- The three 5 m reference areas (kaimai, karangahake, tutamoe) are not published: their reaches have
  no clean link to the national ones (only 82-87 % lie within 150 m of a national reach). Their
  national regions (Waikato, Bay of Plenty, Northland) carry the rain.

## Refresh `rain-inputs` after a national rebuild

After `nz score` / `nz viewer` change the reaches, rebuild the weights if the reach table changed,
then rebuild and push the inputs. `rain inputs` copies `network.npz` when it is current and otherwise
builds it from `segments_table.pkl`; it refuses inputs whose reach counts differ from the viewer
data in `data/viewer_nz/regions_index.json`, and a missing or stale scaling zone file (rf-t26.16).
Refresh the inputs also after a change of `[[scaling.zones]]` (after `rain zones`).

```bash
uv run river-finder rain weights --grid ecmwf          # only if segments_table.pkl changed (~2 min)
uv run river-finder rain zones                         # catchment -> scaling zone (~10 s)
rm -rf /tmp/rain-inputs
uv run river-finder rain inputs --out /tmp/rain-inputs # ~35 MB; prints the regions and their reach counts
cd /tmp/rain-inputs
git init -q -b rain-inputs
git add -A
git commit -q -m "rain inputs: national build of $(jq -r .national_build.segments_table.mtime manifest.json)"
git push --force https://github.com/Sean-McConnachie/river-finder.git HEAD:refs/heads/rain-inputs
gh workflow run rain.yml -R Sean-McConnachie/river-finder   # new rain files now, not at the next :23
```

Push the inputs and run the job when the new site build is deployed (`web pages --push`): until both
match, the site sees a different `n` and hides the rain layer. The force-push keeps one commit, so
old inputs do not pile up in the repository.

## Pause, resume, check

- Pause: Actions tab -> Live rain -> `...` -> Disable workflow, or `gh workflow disable rain.yml`.
  Resume: `gh workflow enable rain.yml`. A run on demand: `gh workflow run rain.yml`.
- Runs: `gh run list --workflow rain.yml`; the newest files: `index.json` on the `rain` branch
  (`generated`, `summary`, `ecmwf.runs`).
- **60-day rule**: in a public repository GitHub disables scheduled workflows after 60 days without
  repository activity, with an email beforehand. Whether the job's own pushes to `rain` (by
  github-actions[bot]) count is not documented, so do not rely on them: any commit to `main`
  resets the clock, and `gh workflow enable rain.yml` brings a disabled job back.

## Local run

The same steps against a scratch directory (writes nothing under `data/`):

```bash
uv run river-finder rain inputs --out /tmp/rain-job
uv run river-finder rain ecmwf --data /tmp/rain-job --no-cache --retries 7
uv run river-finder rain totals --data /tmp/rain-job
uv run river-finder rain publish --data /tmp/rain-job --out /tmp/rain-out
```

Clones and `git fetch` also download the `rain` (~2 MB, new every hour) and `rain-inputs` (~35 MB)
branches. To skip them in a checkout (git 2.29+):
`git config --add remote.origin.fetch '^refs/heads/rain'` and the same with `'^refs/heads/rain-inputs'`.

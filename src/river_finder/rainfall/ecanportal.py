"""Environment Canterbury's Open Data Portal (rf-t26.4): ECan's rainfall sites and their hourly totals.

ECan's Hilltop server (``wateruse.ecan.govt.nz``) reset every https connection from the development
container on 2026-11-21 and plain http does not leave it, so ECan's gauges come from its Open Data
Portal, ``https://data.ecan.govt.nz/`` (``kind = "ecan-portal"`` in ``[gauges.councils.ecan]``). A
request is ``data/<method id>/<collection>/<dataset name>/<format>?<params>``; only the method id
matters (any path words answer the same), formats are CSV, XML, JSON and JSONP. The dataset pages
that document the parameters sit behind a click-through data agreement, which was not accepted here;
the parameters were found by probing the data URLs (a wrong parameter name answers HTTP 500, a
wrong value an empty table). Requests used:

``data/51/Rainfall/Rainfall%20summary%20by%20area/CSV?Sites=All`` (or ``North``, ``South``)
    every rainfall telemetry site, one row each: ``Site Name, Last Sample, Last Hour, RainToday,
    -1 Day .. -7 Day, Total Rainfall, SITE_NO, ShortName, Sub_text, SiteOwner, Location (N | S),
    WGS84_Longitude, WGS84_Latitude, OwnerLogo``. 110 sites on 2026-11-21: ECan 93, Christchurch
    City Council (CCC) 11, Earth Sciences New Zealand (ESNZ) 6;
``data/78/Rainfall/Rainfall%20for%20individual%20site/CSV?SiteNo=<n>&Period=<p>``
    ``site_no, DateTime, RainfallTotal``: hourly totals for ``Period`` = ``2_Days``, ``1_Week``,
    ``2_Weeks`` or ``1_Month`` (a calendar month: 742 rows on 2026-11-22), back from the site's
    last whole hour: 47 rows for ``2_Days``, 167 for ``1_Week``, whatever the time of the request.
    ``3_Months`` and ``1_Year`` give daily totals, other values an empty table. No quality codes:
    the portal calls its values provisional, so every hour is ``raw``;
``data/134/Rainfall/Rainfall%20summary%20for%20site/CSV?SiteNo=<n>``
    one site's row of the summary (not used).

**Times.** The CSV writes ``22/11/2026 9:00:00 AM`` without a zone, in NZ standard time as ECan
states for its rainfall pages ("times are in NZ standard time; during daylight saving add one
hour"). Checked on 2026-11-21 at 21:22Z: the site list's newest samples (09:00-09:15) were 7-22 min
old read as NZST and none was in the future; read as NZDT they would all be 67-82 min old. The JSON
and XML forms append the server's current offset (``2026-11-22T09:00:00+13:00`` in summer), which is
a .NET serialisation of the same wall-clock value, not the zone of the data: they are not used.
Hourly totals are stamped at the end of their hour, a few milliseconds either side (``9:59:59 AM``,
``12:00:00 PM``; JSON ``09:59:59.943``): stamps are rounded to the nearest hour, so a row stamped
``10:00`` NZST is the hour ending 22:00Z the day before (:func:`.gauges.hours_from_records`,
``total-end``). The newest row is the hour ending at the site's last sample when that is on the
hour; a part hour after it is not given (it comes next run: ``refetch_hours``).

**Licence.** The portal: "All data unless specifically stated is licensed under a Creative Commons
Attribution 4.0 International License"; its data terms ask that Environment Canterbury is named as
the source, without ECan or Metro logos, no advertising in applications without permission, and
that data shown on a public site is kept current or removed. CCC and ESNZ gauges are served by ECan
under the same page; their owner is kept per gauge (``owner``).
"""

from __future__ import annotations

import csv
import io
import math
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .config import CouncilConfig, RainConfig
from .fetch import FetchError, Http, NotFound
from .hilltop import Pacer, _nz_lonlat, to_utc

SITES = (51, "Rainfall/Rainfall summary by area")
HOURLY = (78, "Rainfall/Rainfall for individual site")
SUMMARY = (134, "Rainfall/Rainfall summary for site")
# hourly periods, shortest first, with the hours each surely spans back from the site's last whole hour: seen
# 2026-11-21 were 46-47 rows for 2_Days, 167 for 1_Week, 334 for 2_Weeks, 742 for 1_Month (31 days); an hour or two
# less is assumed, and a month of 28 days
PERIODS = (("2_Days", 45), ("1_Week", 7 * 24 - 3), ("2_Weeks", 14 * 24 - 3), ("1_Month", 28 * 24 - 3))
OWNERS = {"ecan": "ECan", "ccc": "CCC", "esnz": "ESNZ"}


class PortalError(Exception):
    """The portal answered with something that is not the expected CSV table."""


def parse_time(s: str) -> datetime:
    """``22/11/2026 9:00:00 AM`` (the CSV form; also ``2026-11-22T09:00:00`` without its offset) as written."""
    s = s.strip()
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        raise PortalError(f"unreadable time {s!r}") from None


def round_hour(t: datetime) -> datetime:
    """The nearest whole hour (``9:59:59.943`` and ``10:00:00.02`` are both 10:00)."""
    return (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)


def _table(body: bytes, need: tuple[str, ...]) -> list[dict]:
    text = body.decode("utf-8-sig", "replace")
    if text.lstrip().startswith("<"):
        raise PortalError(f"not CSV: {text.strip()[:120]!r}")
    rows = list(csv.DictReader(io.StringIO(text)))
    head = rows[0].keys() if rows else next(csv.reader(io.StringIO(text)), [])
    missing = [k for k in need if k not in head]
    if missing:
        raise PortalError(f"CSV without column(s) {missing}: header {list(head)[:12]}")
    return rows


def _f(s: str | None) -> float:
    try:
        return float(s) if s is not None and s.strip() else math.nan
    except ValueError:
        return math.nan


def owner_name(s: str) -> str:
    """``Ecan`` / ``ECAN`` -> ``ECan``; other owners as written."""
    return OWNERS.get(s.strip().lower(), s.strip())


@dataclass
class PortalSite:
    site_no: str
    name: str
    lon: float = math.nan       # WGS84
    lat: float = math.nan
    last_sample: datetime | None = None   # as written (NZST)
    owner: str = ""
    area: str = ""              # N | S

    @property
    def located(self) -> bool:
        return math.isfinite(self.lat) and math.isfinite(self.lon)


def parse_sites(body: bytes) -> list[PortalSite]:
    """The site list (``data/51 ... CSV?Sites=All``)."""
    out = []
    for r in _table(body, ("Site Name", "SITE_NO", "Last Sample", "WGS84_Longitude", "WGS84_Latitude")):
        no, name = (r.get("SITE_NO") or "").strip(), (r.get("Site Name") or "").strip()
        if not no or not name:
            continue
        lat, lon = _nz_lonlat(_f(r.get("WGS84_Latitude")), _f(r.get("WGS84_Longitude")))
        last = parse_time(r["Last Sample"]) if (r.get("Last Sample") or "").strip() else None
        out.append(PortalSite(no, name, lon, lat, last, owner_name(r.get("SiteOwner") or ""),
                              (r.get("Location") or "").strip()))
    return out


@dataclass
class HourlyTable:
    """One ``data/78`` answer: hour-end stamps (rounded, as written: NZST) and mm."""

    times: list[datetime]
    mm: np.ndarray
    off_hour: int = 0        # stamps more than a minute from the hour they were rounded to
    sites: tuple[str, ...] = ()   # site numbers in the answer (one expected)


def parse_hourly(body: bytes) -> HourlyTable:
    rows = _table(body, ("DateTime", "RainfallTotal"))
    times, vals, off, nos = [], [], 0, {}
    for r in rows:
        t = parse_time(r["DateTime"])
        h = round_hour(t)
        off += abs((t - h).total_seconds()) > 60
        times.append(h)
        vals.append(_f(r.get("RainfallTotal")))
        nos.setdefault((r.get("site_no") or "").strip(), None)
    return HourlyTable(times, np.asarray(vals, dtype=np.float64), off, tuple(nos))


def period_for(hours: float) -> tuple[str, int]:
    """The shortest hourly period giving ``hours`` hours back from a site's last whole hour (the longest one
    when none does)."""
    for p, h in PERIODS:
        if hours <= h:
            return p, h
    return PERIODS[-1]


class PortalClient:
    """ECan's Open Data Portal. ``http`` retries 429/5xx and network errors (:class:`.fetch.Http`)."""

    def __init__(self, base_url: str, http: Http | None = None, pacer: Pacer | None = None):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.http, self.pacer = http or Http(), pacer or Pacer()

    def url(self, method: tuple[int, str], fmt: str = "CSV", **params) -> str:
        path = f"data/{method[0]}/{urllib.parse.quote(method[1])}/{fmt}"
        return self.base_url + path + ("?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
                                       if params else "")

    def fetch(self, method: tuple[int, str], **params) -> bytes:
        self.pacer.wait()
        return self.http.get(self.url(method, **params))

    def sites(self, which: str = "All") -> list[PortalSite]:
        return parse_sites(self.fetch(SITES, Sites=which))

    def hourly(self, site_no: str, period: str) -> HourlyTable:
        return parse_hourly(self.fetch(HOURLY, SiteNo=site_no, Period=period))


# --- a council run --------------------------------------------------------------------------------
def run_portal(cfg: RainConfig, c: CouncilConfig, first: int, last: int, refetch_from: int, now_s: float,
               sites_only: bool, http: Http, pacer: Pacer, log, old: dict):
    """The ECan counterpart of :func:`.gauges.run_council` (same gauges, cache files and states)."""
    from .gauges import CouncilRun, Gauge, _iso

    gc = cfg.gauges
    run = CouncilRun(c.id)
    errors = []
    for b in c.base_urls:
        cl = PortalClient(b, http, pacer)
        try:
            sites = cl.sites("All")
            break
        except (FetchError, NotFound, PortalError) as e:
            errors.append(f"{cl.url(SITES, Sites='All')}: {e}")
    else:
        run.status = "failed: " + "; ".join(errors)
        log(f"{c.id}: the portal's site list failed: {run.status}")
        return run
    run.warnings += [f"tried {e}" for e in errors]
    run.base_url, run.hts, run.measurement = cl.base_url, f"data/{HOURLY[0]}", "RainfallTotal (hourly)"
    cut = now_s - gc.inactive_days * 86400
    owners: dict[str, int] = {}
    for s in sites:
        g = Gauge(f"{c.id}:{s.name}", c.id, s.name, "RainfallTotal", s.lon, s.lat, site_no=s.site_no, owner=s.owner,
                  units="mm", interpolation="hourly total", datasource="Rainfall for individual site", raw=True,
                  checked=_iso(now_s))
        if s.last_sample is not None:
            t, st = to_utc([s.last_sample], c.timezone)
            run.times.add(st)
            g.record_last, g.active = _iso(float(t[0])), float(t[0]) >= cut
            if t[0] > now_s + 600:
                run.warnings.append(f"{s.name}: last sample {s.last_sample} is in the future read as {c.timezone}")
        owners[s.owner] = owners.get(s.owner, 0) + 1
        run.gauges.append(g)
    run.extra["owners"] = owners
    unplaced = sum(1 for s in sites if not s.located)
    log(f"{c.id}: {cl.url(SITES, Sites='All')} -> {len(sites)} sites ({sum(g.active for g in run.gauges)} active"
        + (f", {unplaced} without a location" if unplaced else "") + f"); owners {owners}")
    if sites_only:
        run.status = "sites only"
        return run
    for i, g in enumerate(run.gauges):
        if not g.active:
            run.skipped += 1
        elif fetch_portal_gauge(cl, c, cfg, g, first, last, refetch_from, run, now_s):
            run.fetched += 1
        if (i + 1) % 25 == 0:
            log(f"{c.id}: {i + 1}/{len(run.gauges)} sites, {http.stats.requests} requests, "
                f"{http.stats.bytes / 1e6:.1f} MB, {len(run.errors)} errors")
    run.status = "ok" if not run.errors else "partial"
    return run


def fetch_portal_gauge(cl: PortalClient, c: CouncilConfig, cfg: RainConfig, g, first: int, last: int,
                       refetch_from: int, run, now_s: float) -> bool:
    """One request per gauge: the shortest period that reaches the oldest hour the cache lacks (at most a
    month back); hours the answer does not reach stay not_fetched. True if the cache was updated."""
    from .gauges import RAW, _iso, _parse_iso, hours_from_records, load_series, merge, plan, save_series, series_path

    gc = cfg.gauges
    path = series_path(cfg.gauges_dir, g)
    cached, meta = load_series(path)
    rec_end = _parse_iso(g.record_last)
    anchor = math.floor((rec_end if rec_end is not None else now_s) / 3600)  # the site's last whole hour
    # hours older than the longest period are out of reach: they alone are no reason for a request
    ask = plan(cached, max(first, anchor - PERIODS[-1][1] + 1), last, refetch_from)
    if not ask:
        return False
    period, span = period_for(anchor - ask[0][0] + 1)
    todo = plan(cached, first, last, refetch_from)  # what the answer may fill: as far back as its rows go
    try:
        tab = cl.hourly(g.site_no, period)
    except (FetchError, NotFound, PortalError) as e:
        run.errors.append(f"{g.name} ({g.site_no}): {period}: {e}")
        return False
    if tab.sites and set(tab.sites) != {g.site_no}:
        run.errors.append(f"{g.name} ({g.site_no}): the answer is for site(s) {list(tab.sites)}")
        return False
    if tab.off_hour:
        run.warnings.append(f"{g.name}: {tab.off_hour} hourly stamps more than a minute off the hour")
    t_s, st = to_utc(tab.times, c.timezone)
    run.times.add(st)
    order = np.argsort(t_s, kind="stable")
    t_s, mm = t_s[order], tab.mm[order]
    # the hours the answer covers: the period back from the last whole hour, or from its first row if earlier.
    # Event-recording gauges (most of ECan's: 0.5 mm tips) have no row for an hour without a record, so an hour
    # in that span without a row is 0 mm, as for Hilltop records (``raw``); after the last sample: outside.
    h0 = anchor - span + 1
    if len(t_s):
        h0 = min(h0, int(-(-int(t_s[0]) // 3600)))
    block, done = cached, 0
    for a, b in todo:
        a = max(a, h0)
        if a > b:
            continue
        hb = hours_from_records(t_s, mm, a, b, rec_start=(h0 - 1) * 3600, rec_end=rec_end, method="raw",
                                max_mm=gc.max_mm_per_hour)
        hb.state[hb.state < RAW] = RAW  # provisional: never "good"
        block = merge(block, hb)
        done += 1
    if not done:
        return False
    keep_from = last - gc.keep_days * 24 + 1
    if block.first < keep_from:
        block = block.slice(keep_from, block.last)
    fetches = list(meta.get("fetches", [])) + [{"at": _iso(now_s), "period": period, "records": len(t_s)}]
    save_series(path, block, {"id": g.id, "council": g.council, "site": g.name, "site_no": g.site_no,
                              "measurement": g.measurement, "timezone": c.timezone, "method": "raw (hour-end totals)",
                              "owner": g.owner, "units": "mm", "record_last": g.record_last,
                              "fetches": fetches[-24:]})
    return True


# --- discovery ------------------------------------------------------------------------------------
def discover_portal(c: CouncilConfig, http: Http, pacer: Pacer, now_s: float, log) -> dict:
    """The site list (how many, located, owners, how old the newest samples are read in each zone) and one
    site's last two days, for ``rain gauges --discover``."""
    out: dict = {"name": c.name, "kind": c.kind, "timezone": c.timezone, "probes": []}
    for b in c.base_urls:
        cl = PortalClient(b, http, pacer)
        p: dict = {"base_url": b, "sites_url": cl.url(SITES, Sites="All")}
        try:
            sites = cl.sites("All")
        except (FetchError, NotFound, PortalError) as e:
            p["sites_error"] = str(e)
            out["probes"].append(p)
            log(f"{c.id}: {p['sites_url']}: {e}")
            continue
        owners: dict[str, int] = {}
        for s in sites:
            owners[s.owner] = owners.get(s.owner, 0) + 1
        p.update(sites=len(sites), sites_located=sum(s.located for s in sites), owners=owners)
        lasts = [s.last_sample for s in sites if s.last_sample is not None]
        for tz in ("NZST", "Pacific/Auckland", "UTC"):
            if lasts:
                age = np.sort((now_s - to_utc(lasts, tz)[0]) / 60.0)
                p[f"newest_sample_age_min_as_{tz}"] = round(float(age[0]), 1)
                p[f"median_sample_age_min_as_{tz}"] = round(float(np.median(age)), 1)
        fresh = max((s for s in sites if s.last_sample is not None), key=lambda s: s.last_sample, default=None)
        if fresh is not None:
            try:
                tab = cl.hourly(fresh.site_no, "2_Days")
                p["sample"] = {"site": fresh.name, "site_no": fresh.site_no, "last_sample": str(fresh.last_sample),
                               "rows": len(tab.times), "off_hour": tab.off_hour,
                               "tail": [[str(t), float(v)] for t, v in zip(tab.times[-5:], tab.mm[-5:], strict=True)]}
            except (FetchError, NotFound, PortalError) as e:
                p["sample_error"] = str(e)
        out["probes"].append(p)
        log(f"{c.id}: {len(sites)} sites ({p['sites_located']} located), owners {owners}; newest sample "
            f"{p.get('newest_sample_age_min_as_NZST')} min old read as NZST")
        break
    return out

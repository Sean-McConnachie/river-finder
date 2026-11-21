"""Hilltop Server client (rf-t26.4): site lists, measurement discovery and time series, as XML.

Hilltop Server (Hilltop Software) is the web service most NZ regional councils publish their time
series through. A request is ``<base>/<file>.hts?Service=Hilltop&Request=<request>&...`` (key case
is free, value case is not) and the answer is XML; a request the server cannot answer (unknown
site, measurement or file) gives ``<HilltopServer><Error>...</Error>``, raised here as
:class:`HilltopError`. Sources: "Hilltop Server (Web Service)" (Hawke's Bay Regional Council,
2016, ``hbrc.govt.nz/assets/Document-Library/Council-Data/20170426-HilltopServerTrimmed.pdf``) and
the request builder and parsers of hilltop-py 2.4 (``hilltoppy/utils.py``, ``web_service.py``,
``mountain_top.py``, Apache-2.0). Requests used:

``Request=MeasurementList`` (no site)
    the measurement names in the file: ``<Measurement Name="Rainfall"/>`` ...
``Request=SiteList&Location=LatLong&Measurement=<m>``
    the sites with that measurement: ``<Site Name="..."><Latitude>-42.45</Latitude>
    <Longitude>171.21</Longitude></Site>`` (NZGD2000; ``Location=Yes`` gives easting/northing instead)
``Request=MeasurementList&Site=<s>``
    per data source: ``<DataSource Name="Rainfall"><TSType>StdSeries</TSType><DataType>SimpleTimeSeries
    </DataType><Interpolation>Incremental</Interpolation><From>..</From><To>..</To><Measurement
    Name="Rainfall"><Units>mm</Units><RequestAs>Rainfall [Rainfall]</RequestAs></Measurement>``
``Request=GetData&Site=<s>&Measurement=<m>&From=<t>&To=<t>&ShowQuality=Yes``
    ``<Hilltop><Measurement SiteName=".."><DataSource ..><ItemInfo ItemNumber="1"><ItemName>Rainfall
    </ItemName><Units>mm</Units></ItemInfo></DataSource><Data DateFormat="Calendar" NumItems="1">
    <E><T>2026-11-20T01:15:00</T><I1>0.5</I1><Q1>600</Q1></E><Gap/><E>..</E></Data></Measurement>``:
    one ``E`` per record (``I1`` the value, ``Q1`` its quality code when asked), ``<Gap/>`` where the
    record has a gap. ``&Method=Total&Interval=1 hour&Alignment=00:00`` makes the server total per hour.
``... &tsType=StdQualSeries``
    the quality-code series of the same measurement (``I1`` = code, see :func:`.gauges.qc_state`).
``Request=Status``, ``Request=CollectionList``
    only for ``rain gauges --discover`` (what files and collections a server has).

**Times.** Hilltop writes times without a zone, in whatever the council stores, and reads the
``From``/``To`` of a request the same way. NEMS asks recorders for NZST (UTC+12 all year), and
Environment Canterbury says its times are NZST; a server on NZ wall-clock time is NZDT (UTC+13) from
the last Sunday of September (02:00 NZST -> 03:00 NZDT) to the first Sunday of April (03:00 NZDT ->
02:00 NZST). :func:`to_utc` takes the council's ``timezone`` (``NZST``, ``Pacific/Auckland`` or
``UTC``); a time that carries an offset (``Z``, ``+12:00``, as in WaterML 2) is read as given. On a
wall clock the hour 02:00-02:59 on the April Sunday happens twice: records arrive in time order, so
a time that goes back inside that hour starts the second (NZST) pass; a lone record in it cannot be
told apart and is read as the first (NZDT) pass. The hour skipped in September cannot occur and is
read as NZST. ``rain gauges --discover`` prints how far each server's newest record is from now,
which shows a server on NZDT in summer (its newest record looks an hour in the future).

Parsing refuses documents with a DTD or entities (no Hilltop answer has one) and falls back to
cp1252 when a server sends bytes that are not the UTF-8 it declares.
"""

from __future__ import annotations

import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from .fetch import Http

NZST = timezone(timedelta(hours=12), "NZST")
AUCKLAND = ZoneInfo("Pacific/Auckland")
TOTAL_PARAMS = {"Method": "Total", "Interval": "1 hour", "Alignment": "00:00"}


class HilltopError(Exception):
    """The server answered, but with an ``<Error>`` or with something that is not Hilltop XML."""


# --- XML ------------------------------------------------------------------------------------------
def parse_root(body: bytes) -> ET.Element:
    """The root element of a Hilltop answer; raises :class:`HilltopError` on ``<Error>`` or non-XML."""
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise HilltopError("answer has a DTD or entities: not a Hilltop answer, not parsed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", body.decode("cp1252", "replace"))
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            snippet = body[:160].decode("utf-8", "replace").replace("\n", " ")
            raise HilltopError(f"not XML: {snippet!r}") from None
    err = root if root.tag == "Error" else root.find("Error")
    if err is not None:
        raise HilltopError((err.text or "").strip() or "error without a message")
    return root


def parse_time(s: str) -> datetime:
    """A Hilltop time (``2026-11-20T01:15:00``, ``2026-11-20 01:15:00``, fractions, optional offset)."""
    return datetime.fromisoformat(s.strip())


def _float(s: str | None) -> float:
    try:
        return float(s) if s is not None and s.strip() else math.nan
    except ValueError:
        return math.nan


def _int(s: str | None) -> int:
    v = _float(s)
    return int(v) if math.isfinite(v) else -1


@dataclass
class HilltopSite:
    name: str
    lat: float = math.nan   # NZGD2000 (WGS84 to within a metre)
    lon: float = math.nan
    easting: float = math.nan
    northing: float = math.nan

    @property
    def located(self) -> bool:
        return math.isfinite(self.lat) and math.isfinite(self.lon)


def _nz_lonlat(lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) if inside a broad NZ box (Chathams included, longitudes 0..360), else NaN: servers write
    0, 0 or swapped values for sites without a location."""
    if math.isfinite(lat) and math.isfinite(lon):
        lon = lon % 360.0
        if -56.0 <= lat <= -28.0 and 160.0 <= lon <= 190.0:
            return lat, lon
    return math.nan, math.nan


def parse_site_list(body: bytes) -> list[HilltopSite]:
    out = []
    for s in parse_root(body).iter("Site"):
        name = (s.get("Name") or s.findtext("Name") or "").strip()
        if not name:
            continue
        lat, lon = _nz_lonlat(_float(s.findtext("Latitude")), _float(s.findtext("Longitude")))
        out.append(HilltopSite(name, lat, lon, _float(s.findtext("Easting")), _float(s.findtext("Northing"))))
    return out


def parse_measurement_names(body: bytes) -> list[str]:
    """Measurement names in a file-level (or site-level) MeasurementList, in order, without repeats."""
    seen: dict[str, None] = {}
    for m in parse_root(body).iter("Measurement"):
        name = (m.get("Name") or "").strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def _bare(name: str) -> str:
    """``"Rainfall [Rainfall]"`` -> ``"rainfall"``: the measurement name without its data-source part."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", name).strip().lower()


def choose_measurement(names: list[str], hints: tuple[str, ...] | list[str]) -> str | None:
    """The first hint (in order) that names a measurement: exactly (any case), else as ``<hint> [<source>]``."""
    for h in hints:
        hl = h.strip().lower()
        exact = [n for n in names if n.lower() == hl]
        if exact:
            return exact[0]
        bracket = [n for n in names if _bare(n) == hl]
        if bracket:
            return bracket[0]
    return None


def rain_like(names: list[str]) -> list[str]:
    """Names that look like rainfall, for discovery reports when no hint matched."""
    return [n for n in names if "rain" in n.lower()]


@dataclass
class DataSourceInfo:
    """One data source of a site (site-level MeasurementList)."""

    datasource: str
    measurement: str
    request_as: str = ""
    units: str = ""
    tstype: str = ""
    data_type: str = ""
    interpolation: str = ""
    first: datetime | None = None   # From / To as the server wrote them (its time zone)
    last: datetime | None = None

    @property
    def request_name(self) -> str:
        return self.request_as or self.measurement


def _time_or_none(s: str | None) -> datetime | None:
    if not s or not s.strip():
        return None
    try:
        return parse_time(s)
    except ValueError:
        return None


def parse_site_measurements(body: bytes) -> list[DataSourceInfo]:
    out = []
    for d in parse_root(body).iter("DataSource"):
        common = dict(tstype=(d.findtext("TSType") or "").strip(), data_type=(d.findtext("DataType") or "").strip(),
                      interpolation=(d.findtext("Interpolation") or "").strip(),
                      first=_time_or_none(d.findtext("From")), last=_time_or_none(d.findtext("To")))
        ms = d.findall("Measurement") or [None]
        for m in ms:
            name = (m.get("Name") if m is not None else None) or d.get("Name") or ""
            out.append(DataSourceInfo(d.get("Name") or name, name.strip(),
                                      (m.findtext("RequestAs") or "").strip() if m is not None else "",
                                      (m.findtext("Units") or "").strip() if m is not None else "", **common))
    return out


def find_source(sources: list[DataSourceInfo], measurement: str) -> DataSourceInfo | None:
    """The data source of ``measurement`` (matched on its name or RequestAs, any case; standard series first)."""
    ml = measurement.lower()
    hits = [s for s in sources if ml in (s.measurement.lower(), s.request_as.lower(), _bare(s.request_as))
            or _bare(s.measurement) == _bare(measurement)]
    hits.sort(key=lambda s: s.tstype not in ("", "StdSeries"))
    return hits[0] if hits else None


@dataclass
class Series:
    """One GetData answer: records in the order the server wrote them, times as written (its time zone)."""

    site: str = ""
    measurement: str = ""
    times: list[datetime] = field(default_factory=list)
    values: np.ndarray = field(default_factory=lambda: np.zeros(0))          # float64, NaN = unreadable
    quality: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))  # int32, -1 = none given
    gap_before: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))  # a <Gap/> right before record i
    gap_at_end: bool = False      # a <Gap/> after the last record (or the only element)
    units: str = ""
    item: str = ""
    data_type: str = ""
    interpolation: str = ""
    tstype: str = ""

    def __len__(self) -> int:
        return len(self.times)


def parse_data(body: bytes) -> Series:
    """A GetData answer (``E`` records with ``T``, ``I<n>`` / ``Value`` and ``Q<n>`` / ``QualityCode``; ``Gap``)."""
    root = parse_root(body)
    m = root.find("Measurement")
    if m is None:
        return Series()
    s = Series(site=m.get("SiteName") or "")
    ds = m.find("DataSource")
    item = 1
    if ds is not None:
        s.measurement = ds.get("Name") or ""
        s.tstype, s.data_type = (ds.findtext("TSType") or "").strip(), (ds.findtext("DataType") or "").strip()
        s.interpolation = (ds.findtext("Interpolation") or "").strip()
        info = ds.find("ItemInfo")
        if info is not None:
            item = max(1, _int(info.get("ItemNumber"))) if info.get("ItemNumber") else 1
            s.item, s.units = (info.findtext("ItemName") or "").strip(), (info.findtext("Units") or "").strip()
    data = m.find("Data")
    times, vals, qual, gaps = [], [], [], []
    pending = False
    for el in [] if data is None else data:
        if el.tag == "Gap":
            pending = True
        elif el.tag == "E":
            t = el.findtext("T")
            if not t:
                continue
            try:
                times.append(parse_time(t))
            except ValueError:
                raise HilltopError(f"unreadable record time {t!r}") from None
            v = el.find(f"I{item}")
            if v is None:
                v = el.find("Value")
            vals.append(_float(v.text if v is not None else None))
            q = el.find(f"Q{item}")
            q = q if q is not None else el.find("QualityCode")
            qual.append(_int(q.text) if q is not None else -1)
            gaps.append(pending)
            pending = False
    s.times, s.values = times, np.asarray(vals, dtype=np.float64)
    s.quality, s.gap_before = np.asarray(qual, dtype=np.int32), np.asarray(gaps, dtype=bool)
    s.gap_at_end = pending
    return s


def parse_status(body: bytes) -> dict:
    """``Request=Status``: the server's top-level fields and its data files (best effort, for discovery)."""
    root = parse_root(body)
    out = {c.tag: (c.text or "").strip() for c in root if len(c) == 0 and (c.text or "").strip()}
    files = []
    for f in root.iter("DataFile"):
        files.append({c.tag: (c.text or "").strip() for c in f if (c.text or "").strip()})
    if files:
        out["data_files"] = files
    return out


def parse_collections(body: bytes) -> list[str]:
    return [c.get("Name") or "" for c in parse_root(body).iter("Collection") if c.get("Name")]


# --- times ----------------------------------------------------------------------------------------
@dataclass
class TimeStats:
    explicit_offset: int = 0   # times that carried their own offset
    ambiguous: int = 0         # Pacific/Auckland: times in the repeated hour when NZDT ends
    second_pass: int = 0       # ... of them, read as the second (NZST) pass
    nonexistent: int = 0       # Pacific/Auckland: times in the hour skipped when NZDT starts (read as NZST)

    def add(self, o: TimeStats) -> None:
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(o, k))

    def to_dict(self) -> dict:
        return dict(vars(self))


def to_utc(times: list[datetime], tz: str) -> tuple[np.ndarray, TimeStats]:
    """Epoch seconds (int64, floored) of ``times`` written in the server's zone ``tz`` (records in time order)."""
    out = np.empty(len(times), dtype=np.int64)
    st = TimeStats()
    prev, second = None, False
    for i, t in enumerate(times):
        if t.tzinfo is not None:
            st.explicit_offset += 1
            out[i] = math.floor(t.timestamp())
            continue
        if tz == "UTC":
            aware = t.replace(tzinfo=UTC)
        elif tz == "NZST":
            aware = t.replace(tzinfo=NZST)
        elif tz == "Pacific/Auckland":
            a, b = t.replace(tzinfo=AUCKLAND, fold=0), t.replace(tzinfo=AUCKLAND, fold=1)
            oa, ob = a.utcoffset(), b.utcoffset()
            if oa == ob:
                aware, second = a, False
            elif oa > ob:  # repeated hour: fold 0 = NZDT (first pass), fold 1 = NZST (second pass)
                st.ambiguous += 1
                if prev is not None and t < prev:
                    second = True
                aware = b if second else a
                st.second_pass += second
            else:  # skipped hour: fold 0 takes the offset before the change, NZST
                st.nonexistent += 1
                aware, second = a, False
        else:
            raise ValueError(f"unknown time zone {tz!r}")
        out[i] = math.floor(aware.timestamp())
        prev = t
    return out, st


def local_time(epoch_s: int, tz: str) -> str:
    """Epoch seconds as the server's wall-clock time, for ``From``/``To`` (``YYYY-MM-DDTHH:MM:SS``)."""
    t = datetime.fromtimestamp(int(epoch_s), UTC)
    if tz == "NZST":
        t = t.astimezone(NZST)
    elif tz == "Pacific/Auckland":
        t = t.astimezone(AUCKLAND)
    elif tz != "UTC":
        raise ValueError(f"unknown time zone {tz!r}")
    return t.strftime("%Y-%m-%dT%H:%M:%S")


# --- client ---------------------------------------------------------------------------------------
def build_url(base_url: str, hts: str, request: str, **params) -> str:
    q = {"Service": "Hilltop", "Request": request, **{k: v for k, v in params.items() if v is not None}}
    return base_url.rstrip("/") + "/" + hts + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


class Pacer:
    """At least ``gap_s`` seconds between the starts of two requests (one per server, shared by its clients)."""

    def __init__(self, gap_s: float = 1.0, clock=time.monotonic, sleep=time.sleep):
        self.gap_s, self.clock, self.sleep = float(gap_s), clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            left = self.gap_s - (self.clock() - self._last)
            if left > 0:
                self.sleep(left)
        self._last = self.clock()


class HilltopClient:
    """One Hilltop file on one server. ``http`` retries 429/5xx and network errors (:class:`.fetch.Http`)."""

    def __init__(self, base_url: str, hts: str, tz: str = "NZST", http: Http | None = None,
                 pacer: Pacer | None = None):
        if tz not in ("NZST", "Pacific/Auckland", "UTC"):
            raise ValueError(f"unknown time zone {tz!r}")
        self.base_url, self.hts, self.tz = base_url, hts, tz
        self.http = http or Http()
        self.pacer = pacer or Pacer()

    def url(self, request: str, **params) -> str:
        return build_url(self.base_url, self.hts, request, **params)

    def fetch(self, request: str, **params) -> bytes:
        self.pacer.wait()
        return self.http.get(self.url(request, **params))

    def status(self) -> dict:
        return parse_status(self.fetch("Status"))

    def collections(self) -> list[str]:
        return parse_collections(self.fetch("CollectionList"))

    def measurement_names(self) -> list[str]:
        return parse_measurement_names(self.fetch("MeasurementList"))

    def sites(self, measurement: str | None = None) -> list[HilltopSite]:
        return parse_site_list(self.fetch("SiteList", Location="LatLong", Measurement=measurement))

    def site_measurements(self, site: str) -> list[DataSourceInfo]:
        return parse_site_measurements(self.fetch("MeasurementList", Site=site))

    def get_data(self, site: str, measurement: str, start_s: int, end_s: int, total: bool = False,
                 quality: bool = False, tstype: str | None = None) -> Series:
        """Records from ``start_s`` to ``end_s`` (epoch seconds, sent as the server's wall-clock time)."""
        params = {"Site": site, "Measurement": measurement, "From": local_time(start_s, self.tz),
                  "To": local_time(end_s, self.tz)}
        if quality:
            params["ShowQuality"] = "Yes"
        if tstype:
            params["tsType"] = tstype
        if total:
            params.update(TOTAL_PARAMS)
        s = parse_data(self.fetch("GetData", **params))
        s.site = s.site or site
        s.measurement = s.measurement or measurement
        return s

    def quality_series(self, site: str, measurement: str, start_s: int, end_s: int) -> Series:
        """The quality-code series (``tsType=StdQualSeries``) over a span; codes in ``values``."""
        return self.get_data(site, measurement, start_s, end_s, tstype="StdQualSeries")

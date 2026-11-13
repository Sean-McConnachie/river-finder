"""Import the river-guide research data (``labels/guide_research.json``) as label sections.

The research file was curated by a separate agent and its exact layout was not fixed in advance,
so this importer is deliberately tolerant. It accepts:

- a list of entries, or an object holding one under ``rivers`` / ``sections`` / ``runs`` / ``data``
  / ``items`` (or region names mapping to lists);
- river entries with a nested ``sections`` list (river-level fields are inherited);
- coordinates as ``{"lat": .., "lon": ..}`` (also ``lng`` / ``latitude`` / ``longitude``),
  ``[lon, lat]`` or ``[lat, lon]`` (told apart by NZ's ranges), ``"lat, lon"`` strings, or flat
  ``put_in_lat`` / ``put_in_lon`` keys;
- coordinate provenance as a boolean (``coords_inferred``, ``inferred``, ``estimated``) or a text
  field (``coord_source``, ``coordinate_source``, ``coords_source``, ``location_source``,
  ``confidence``), per section or per point.

Each entry becomes a ``labels.Section`` with ``coords`` = "sourced", "inferred" or "none" and the
river-guide ``region`` normalised to the names in ``national.FILTER_ORDER``. Fields this module
does not know are kept in ``Section.extra``; ``summary()`` lists them so the mapping can be
extended once the real file is in hand.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .labels import Section
from .regions import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "labels" / "guide_research.json"

_CONTAINERS = ("rivers", "sections", "runs", "data", "items", "results", "entries")
_RIVER = ("river", "river_name", "rec2_name", "waterway")
_SECTION = ("section", "section_name", "run", "run_name", "reach")
_REGION = ("region", "guide_region", "riverguide_region", "area", "region_name")
_GRADE = ("grade", "class", "difficulty", "grade_range")
_PUT_IN = ("put_in", "putin", "put_in_coords", "put_in_location", "start", "start_coords", "top")
_TAKE_OUT = ("take_out", "takeout", "take_out_coords", "take_out_location", "end", "end_coords", "bottom")
_GRADIENT = ("gradient", "gradient_m_per_km", "avg_gradient", "average_gradient", "gradient_m_km")
_LENGTH = ("length_km", "length", "distance_km", "distance")
_STATUS = ("status", "runnable")
_SOURCE = ("source", "sources", "url", "urls", "reference", "references", "notes")
_INFERRED_BOOL = ("coords_inferred", "coordinates_inferred", "inferred", "estimated", "inferred_coordinates",
                  "coords_estimated", "approximate")
_PROVENANCE = ("coord_source", "coords_source", "coordinate_source", "coordinates_source", "location_source",
               "coord_provenance", "coordinate_provenance", "provenance", "confidence", "coord_confidence",
               "coordinate_confidence", "source_type")
_KNOWN = set(_RIVER + _SECTION + _REGION + _GRADE + _PUT_IN + _TAKE_OUT + _GRADIENT + _LENGTH + _STATUS + _SOURCE
             + _INFERRED_BOOL + _PROVENANCE + ("name", "sections", "lat", "lon", "lng"))

# research region name (normalised) -> river-guide region (national.FILTER_ORDER)
_REGION_ALIASES = {
    "northland": "Northland", "auckland": "Auckland", "coromandel": "Waikato", "waikato": "Waikato",
    "bay of plenty": "Bay of Plenty", "bop": "Bay of Plenty", "gisborne": "Gisborne", "east cape": "Gisborne",
    "hawkes bay": "Hawke's Bay", "hawke's bay": "Hawke's Bay", "taranaki": "Taranaki",
    "wanganui": "Wanganui", "whanganui": "Wanganui", "manawatu": "Manawatu", "manawatu-wanganui": "Manawatu",
    "manawatu-whanganui": "Manawatu", "wellington": "Wellington", "wairarapa": "Wellington",
    "tasman": "Tasman", "nelson": "Tasman", "golden bay": "Tasman", "nelson tasman": "Tasman",
    "marlborough": "Marlborough", "kaikoura": "Canterbury", "west coast": "West Coast", "westland": "West Coast",
    "buller": "West Coast", "canterbury": "Canterbury", "otago": "Otago", "southland": "Southland",
    "fiordland": "Southland", "central plateau": "Waikato", "taupo": "Waikato", "rotorua": "Bay of Plenty",
}


def _norm(s: str) -> str:
    s = s.lower().replace("ā", "a").replace("ē", "e").replace("ī", "i").replace("ō", "o").replace("ū", "u")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z' -]", " ", s)).strip()


def guide_region(name: str | None) -> str | None:
    """Map a research region name onto the river-guide regions; None if unknown."""
    if not name:
        return None
    n = _norm(str(name))
    if n in _REGION_ALIASES:
        return _REGION_ALIASES[n]
    for k in sorted(_REGION_ALIASES, key=len, reverse=True):  # "Upper Waikato" -> Waikato
        if re.search(rf"\b{re.escape(k)}\b", n):
            return _REGION_ALIASES[k]
    return None


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return None


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _lonlat(a: float, b: float) -> tuple[float, float] | None:
    """Order two numbers as (lon, lat) for New Zealand (lat -29..-53, lon 165..180 or -180..-175)."""
    def is_lat(v):
        return -53.0 <= v <= -29.0

    def is_lon(v):
        return 165.0 <= v <= 180.0 or -180.0 <= v <= -175.0

    if is_lon(a) and is_lat(b):
        return (a, b)
    if is_lat(a) and is_lon(b):
        return (b, a)
    return None


def parse_point(v) -> tuple[float, float] | None:
    """A coordinate in any of the accepted shapes -> (lon, lat), or None."""
    if v is None:
        return None
    if isinstance(v, dict):
        lat = _num(_first(v, ("lat", "latitude", "y")))
        lon = _num(_first(v, ("lon", "lng", "long", "longitude", "x")))
        if lat is not None and lon is not None:
            return _lonlat(lon, lat)
        inner = _first(v, ("coords", "coordinates", "point", "location", "latlon", "lonlat"))
        return parse_point(inner) if inner is not None else None
    if isinstance(v, list | tuple) and len(v) >= 2:
        a, b = _num(v[0]), _num(v[1])
        return _lonlat(a, b) if a is not None and b is not None else None
    if isinstance(v, str):
        nums = re.findall(r"-?\d+(?:\.\d+)?", v)
        if len(nums) >= 2:
            a, b = float(nums[0]), float(nums[1])
            if "s" in v.lower() and a > 0 and -53 <= -a <= -29:  # "37.5 S, 175.9 E"
                a = -a
            return _lonlat(a, b)
    return None


def _flat_point(d: dict, prefix: str) -> tuple[float, float] | None:
    for p in (prefix, prefix.replace("_", "")):
        lat = _num(_first(d, (f"{p}_lat", f"{p}_latitude")))
        lon = _num(_first(d, (f"{p}_lon", f"{p}_lng", f"{p}_long", f"{p}_longitude")))
        if lat is not None and lon is not None:
            return _lonlat(lon, lat)
    return None


_INFERRED_WORDS = ("infer", "estimat", "approx", "guess", "derived", "interpolat", "low", "medium", "model", "assum")
_SOURCED_WORDS = ("source", "guide", "gps", "survey", "official", "doc", "verified", "measured", "high", "exact",
                  "published", "map", "riverguide", "whitewater")


def _provenance(d: dict) -> str | None:
    """'inferred', 'sourced' or None (not stated) from a section or point dict."""
    b = _first(d, _INFERRED_BOOL)
    if isinstance(b, bool):
        return "inferred" if b else "sourced"
    if isinstance(b, str) and b.lower() in ("true", "yes", "false", "no"):
        return "inferred" if b.lower() in ("true", "yes") else "sourced"
    t = _first(d, _PROVENANCE)
    if isinstance(t, str):
        t = t.lower()
        if any(w in t for w in _INFERRED_WORDS):
            return "inferred"
        if any(w in t for w in _SOURCED_WORDS):
            return "sourced"
    return None


def _status(v) -> str:
    if isinstance(v, bool):
        return "runnable" if v else "not_runnable"
    t = str(v or "").lower()
    return "not_runnable" if re.search(r"\b(not|un|non)[ _-]?runnable\b|\bunrunnable\b", t) else "runnable"


def _entries(obj, region_hint: str | None = None):
    """Yield (entry dict, region hint) for every river or section entry in the file."""
    if isinstance(obj, list):
        for e in obj:
            if isinstance(e, dict):
                yield e, region_hint
        return
    if not isinstance(obj, dict):
        return
    for k in _CONTAINERS:
        if isinstance(obj.get(k), list | dict):
            yield from _entries(obj[k], region_hint)
            return
    # {"Bay of Plenty": [...], "Waikato": [...]}
    keyed = {k: v for k, v in obj.items() if isinstance(v, list | dict)}
    if keyed and all(guide_region(k) for k in keyed):
        for k, v in keyed.items():
            yield from _entries(v, k)
        return
    if _first(obj, _RIVER + ("name",)):
        yield obj, region_hint


def _section(d: dict, parent: dict, region_hint: str | None) -> Section | None:
    get = lambda keys: _first(d, keys) if _first(d, keys) is not None else _first(parent, keys)  # noqa: E731
    river = get(_RIVER) or (parent.get("name") if parent is not d else None) or d.get("name")
    if not river:
        return None
    sec = _first(d, _SECTION) or (d.get("name") if parent is not d and d.get("name") != river else None)
    pi_raw, to_raw = _first(d, _PUT_IN), _first(d, _TAKE_OUT)
    put_in = parse_point(pi_raw) or _flat_point(d, "put_in")
    take_out = parse_point(to_raw) or _flat_point(d, "take_out")
    def point_prov(raw, name):
        flag = _first(d, (f"inferred_{name}_coords", f"{name}_inferred", f"{name}_coords_inferred"))
        if isinstance(flag, bool):
            return "inferred" if flag else "sourced"
        return _provenance(raw) if isinstance(raw, dict) else None

    pi_prov = point_prov(pi_raw, "put_in") if put_in else None
    to_prov = point_prov(to_raw, "take_out") if take_out else None
    prov = [p for p in (pi_prov, to_prov) if p]
    sec_prov = _provenance(d) or (_provenance(parent) if parent is not d else None)
    if not (put_in or take_out):
        coords = "none"
    elif "inferred" in prov or (not prov and sec_prov == "inferred"):
        coords = "inferred"
    elif prov or sec_prov == "sourced":
        coords = "sourced"
    else:
        coords = "unstated"
    region_raw = _first(d, ("region_riverguide",)) or get(_REGION) or region_hint
    src = get(_SOURCE)
    src = "; ".join(map(str, src)) if isinstance(src, list) else (str(src) if src else "")
    grade = get(_GRADE)
    extra = {k: v for k, v in {**(parent if parent is not d else {}), **d}.items() if k not in _KNOWN}
    extra.update({"section": sec, "region_raw": region_raw, "put_in_coords_from": pi_prov,
                  "take_out_coords_from": to_prov,
                  "gradient_m_per_km": _num(get(_GRADIENT)), "length_km": _num(get(_LENGTH))})
    return Section(region=guide_region(region_raw) or "", river=str(river).strip(), status=_status(get(_STATUS)),
                   grade=str(grade) if grade is not None else None, put_in=put_in, take_out=take_out,
                   source=src or "guide research", coords=coords, extra=extra)


def load_research(path: Path | None = None) -> list[Section]:
    path = Path(path or DEFAULT_PATH)
    if not path.exists():
        return []
    obj = json.loads(path.read_text())
    out = []
    for e, hint in _entries(obj):
        subs = e.get("sections")
        if isinstance(subs, list) and subs and all(isinstance(s, dict) for s in subs):
            out += [s for s in (_section(sub, e, hint) for sub in subs) if s]
        else:
            s = _section(e, e, hint)
            if s:
                out.append(s)
    return out


def summary(sections: list[Section]) -> dict:
    """What the importer made of the file: counts and the fields it did not recognise."""
    unknown = Counter(k for s in sections for k in s.extra if k not in
                      ("section", "region_raw", "gradient_m_per_km", "length_km", "put_in_coords_from",
                       "take_out_coords_from"))
    pub = [sourced_only(s) for s in sections]
    return {
        "sections": len(sections),
        "rivers": len({(s.region, s.river) for s in sections}),
        "coords": dict(Counter(s.coords for s in sections)),
        "status": dict(Counter(s.status for s in sections)),
        "published_only_extents": dict(Counter(p.extra["extent_from"] if p else "dropped" for p in pub)),
        "regions": dict(Counter(s.region or "?" for s in sections)),
        "unmapped_regions": sorted({str(s.extra.get("region_raw")) for s in sections if not s.region}),
        "unknown_fields": dict(unknown.most_common()),
    }


def sourced_only(s: Section) -> Section | None:
    """The section using published coordinates only, or None if they don't define an extent.

    Inferred points are dropped. A published put-in with an inferred take-out keeps its put-in and
    runs downstream for the stated length; without a stated length the section is dropped.
    """
    from dataclasses import replace

    ex = s.extra or {}
    if s.coords not in ("inferred",):
        return replace(s, extra={**ex, "extent_from": "as given"})
    pi = s.put_in if ex.get("put_in_coords_from") != "inferred" else None
    to = s.take_out if ex.get("take_out_coords_from") != "inferred" else None
    if pi and to:
        return replace(s, coords="sourced", extra={**ex, "extent_from": "published put-in and take-out"})
    if pi and ex.get("length_km"):
        return replace(s, take_out=None, coords="sourced", extra={**ex, "extent_from": "published put-in + length"})
    return None

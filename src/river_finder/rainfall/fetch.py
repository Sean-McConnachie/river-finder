"""HTTP for the rainfall sources: byte-range GETs from public buckets, with retries, mirrors and a tally.

- :class:`Http` does one GET (whole object, a byte range, or the last ``n`` bytes) and retries
  transient failures (HTTP 429/5xx, network errors, bodies cut short) with exponential backoff and jitter. S3 answers
  ``503 SlowDown`` when a prefix is busy: on 2026-11-19 the ECMWF bucket refused every request for
  several minutes in a row. ``Http.stats`` counts requests, bytes and seconds for the CLI report.
- :class:`Bucket` is a list of mirrors of one public bucket (S3 or Google Cloud Storage, both
  serve the S3 v1 XML listing). It uses the first mirror that answers and stays on it; a mirror
  that still fails after its retries hands over to the next one. A 404 is an answer, not a
  failure: it raises :class:`NotFound` without trying other mirrors.
- :class:`RangeFile` gives one remote object the ``size`` / ``cat_file`` interface that
  ``omfiles.OmFileReader.from_fsspec`` reads through. It fetches the last 512 kB once (the trailer,
  variable metadata and lookup tables of an ``.om`` file sit at the end) and reads a little behind
  and ahead on every miss, so one variable of an Open-Meteo file costs two requests (~0.7 MB)
  instead of ~30.

Tests replace ``Http.get`` (``FakeHttp`` in ``tests/test_rainfall_ecmwf.py``); nothing here needs
credentials.
"""

from __future__ import annotations

import http.client
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

RETRY_STATUS = {429, 500, 502, 503, 504}


class NotFound(Exception):
    """The object (or listing prefix) does not exist on the mirror asked."""


class FetchError(Exception):
    """No mirror answered after all retries."""


@dataclass
class Stats:
    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0
    retries: int = 0
    by_host: dict = field(default_factory=dict)

    def add(self, url: str, nbytes: int, seconds: float) -> None:
        self.requests += 1
        self.bytes += nbytes
        self.seconds += seconds
        h = self.by_host.setdefault(urllib.parse.urlsplit(url).netloc, {"requests": 0, "bytes": 0})
        h["requests"] += 1
        h["bytes"] += nbytes

    def to_dict(self) -> dict:
        return {"requests": self.requests, "bytes": self.bytes, "seconds": round(self.seconds, 2),
                "retries": self.retries, "by_host": self.by_host}


class Http:
    """GET with byte ranges and retries. ``start``/``end`` are a half-open byte range ``[start, end)``."""

    def __init__(self, timeout: float = 60.0, retries: int = 5, backoff: float = 1.0, log=None, sleep=time.sleep):
        self.timeout, self.retries, self.backoff, self.log, self.sleep = timeout, retries, backoff, log, sleep
        self.stats = Stats()

    def _once(self, url: str, headers: dict) -> tuple[bytes, dict]:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read(), dict(r.headers)

    def request(self, url: str, start: int | None = None, end: int | None = None,
                suffix: int | None = None) -> tuple[bytes, dict]:
        headers = {"User-Agent": "river-finder-rainfall"}
        if suffix is not None:
            headers["Range"] = f"bytes=-{int(suffix)}"
        elif start is not None or end is not None:
            s = int(start or 0)
            headers["Range"] = f"bytes={s}-" + ("" if end is None else str(int(end) - 1))
        last = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                body, hdrs = self._once(url, headers)
                self.stats.add(url, len(body), time.monotonic() - t0)
                return body, hdrs
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(url) from None
                if e.code not in RETRY_STATUS:
                    raise FetchError(f"{url}: HTTP {e.code}") from None
                last = f"HTTP {e.code}"
            # http.client.HTTPException: a body cut short (IncompleteRead: S3 dropped one 2026-11-22 in the
            # middle of a ~0.5 MB range) or a malformed answer; transient like a reset connection
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
                if re.search(r"Tunnel connection failed: 40[37]", str(e)):
                    # an HTTPS proxy that refuses the host: a policy, not a transient failure
                    raise FetchError(f"{url}: refused by the proxy ({e})") from None
                last = f"{type(e).__name__}: {e}"
            if attempt < self.retries:
                self.stats.retries += 1
                wait = self.backoff * 2 ** attempt * (0.5 + random.random())
                if self.log:
                    self.log(f"  {last} from {urllib.parse.urlsplit(url).netloc}, retry in {wait:.1f} s")
                self.sleep(wait)
        raise FetchError(f"{url}: {last} after {self.retries + 1} attempts")

    def get(self, url: str, start: int | None = None, end: int | None = None) -> bytes:
        return self.request(url, start, end)[0]

    def tail(self, url: str, n: int) -> tuple[bytes, int]:
        """The last ``n`` bytes (fewer if the object is smaller) and the object's total size."""
        body, hdrs = self.request(url, suffix=n)
        cr = {k.lower(): v for k, v in hdrs.items()}.get("content-range", "")
        m = re.search(r"/(\d+)\s*$", cr)
        return body, int(m.group(1)) if m else len(body)


_KEY = re.compile(r"<Key>([^<]*)</Key>")


class Bucket:
    """Mirrors of one public bucket; ``key``s are object names relative to each base URL."""

    def __init__(self, bases: list[str] | tuple[str, ...], http: Http | None = None):
        if not bases:
            raise ValueError("a bucket needs at least one base URL")
        self.bases = [b if b.endswith("/") else b + "/" for b in bases]
        self.http = http or Http()
        self.active = 0

    @property
    def base(self) -> str:
        return self.bases[self.active]

    def _try(self, fn):
        errors = []
        for i in range(self.active, len(self.bases)):
            try:
                out = fn(self.bases[i])
                self.active = i
                return out
            except FetchError as e:
                errors.append(str(e))
        raise FetchError("; ".join(errors))

    def url(self, key: str) -> str:
        return self.base + key

    def get(self, key: str, start: int | None = None, end: int | None = None) -> bytes:
        return self._try(lambda b: self.http.get(b + key, start, end))

    def tail(self, key: str, n: int) -> tuple[bytes, int]:
        return self._try(lambda b: self.http.tail(b + key, n))

    def list(self, prefix: str) -> list[str]:
        """Every key under ``prefix`` (S3 v1 listing, which GCS serves too; follows the markers)."""
        def run(base: str) -> list[str]:
            keys, marker = [], ""
            while True:
                q = {"prefix": prefix, **({"marker": marker} if marker else {})}
                xml = self.http.get(base + "?" + urllib.parse.urlencode(q)).decode("utf-8", "replace")
                page = [_unescape(k) for k in _KEY.findall(xml)]
                keys += page
                if "<IsTruncated>true</IsTruncated>" not in xml or not page:
                    return keys
                m = re.search(r"<NextMarker>([^<]*)</NextMarker>", xml)
                marker = _unescape(m.group(1)) if m else page[-1]
        return self._try(run)


def _unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


class RangeFile:
    """One remote object read in byte ranges through a small cache (the fsspec subset omfiles uses)."""

    def __init__(self, bucket: Bucket, key: str, tail: int = 512 * 1024, readahead: int = 192 * 1024,
                 readbehind: int = 16 * 1024):
        self.bucket, self.key, self.readahead, self.readbehind = bucket, key, readahead, readbehind
        body, self._size = bucket.tail(key, tail)
        self._chunks: list[tuple[int, bytes]] = [(self._size - len(body), body)]

    def size(self, path: str | None = None) -> int:
        return self._size

    def _cached(self, s: int, e: int) -> bytes | None:
        for c0, b in self._chunks:
            if c0 <= s and e <= c0 + len(b):
                return b[s - c0:e - c0]
        return None

    def cat_file(self, path: str | None = None, start: int | None = None, end: int | None = None, **kw) -> bytes:
        s = 0 if start is None else (start + self._size if start < 0 else start)
        e = self._size if end is None else min(end + self._size if end < 0 else end, self._size)
        if e <= s:
            return b""
        hit = self._cached(s, e)
        if hit is not None:
            return hit
        a = max(0, s - self.readbehind)  # metadata reads often step back a little
        stop = min(max(e, s + self.readahead), self._size)
        body = self.bucket.get(self.key, a, stop)
        self._chunks.append((a, body))
        return body[s - a:e - a]

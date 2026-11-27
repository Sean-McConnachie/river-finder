"""The one-pass extraction spec (lidarspec.py, docs/lidar-extraction-spec.md): every field and file today's consumers
read is produced by the 1 m rebuild or listed as replaced or dropped; the field table, the stored layouts, the reach
ids and the doc agree with the code."""

from pathlib import Path

import numpy as np
import pandas as pd

from river_finder import lidarpipe as P
from river_finder import lidarspec as S
from river_finder import ml, models, nz
from river_finder import xsec1m as X1
from river_finder.rainfall.pipeline import TABLE_COLUMNS

DOC = Path(__file__).resolve().parents[1] / "docs" / "lidar-extraction-spec.md"


def test_every_consumer_field_is_produced_or_replaced():
    assert S.unmet() == {}
    for old, new in S.REPLACED.items():
        assert new in S.BY_NAME, (old, new)
        assert old not in S.BY_NAME
    assert not set(S.DROPPED) & set(S.BY_NAME)


def test_consumer_lists_follow_the_code():
    have = S.consumer_fields()
    table = set(models._RAW) | {"valley_width_m"} | set(models._MAF_COLS)
    assert table <= have["models (national_table, map_section, cards)"]
    assert set(TABLE_COLUMNS) <= have["rainfall (weights, network, zones, live)"]
    assert set(ml.GEO_COLS) <= have["ml (ml_table, labels, strata, audit)"]
    feats = have["scoring.FEATURES (cards v1-v4, logit13)"]
    assert {"grad_s", "steepest_50m_m_per_km", "melton", "height_to_width"} <= feats
    assert {"x1_ffbf_med", "x1_wall_lo_med", "x1_rough"} <= have["rf-lxa.4 keep list (xsec1mstudy.FEATURES1M)"]


def test_fields_are_well_formed():
    names = [f.name for f in S.FIELDS]
    assert len(names) == len(set(names))
    for f in S.FIELDS:
        assert f.stage in S.STAGES and f.group in S.GROUPS and f.dtype in S.DTYPES, f
        assert f.what, f.name
        assert not (f.store and f.stage == "2m"), f.name        # nothing at 2 m is kept, so it cannot be recomputed
    assert set(S.counts()) == set(S.STAGES)


def test_x1_fields_are_what_xsec1m_writes():
    n = 4
    cols = {c: np.full(n, 0.5) for c in X1.SCALARS}
    for h in X1.STAGES:
        tg = X1.stage_tag(h)
        for k in ("w", "a", "p", "dl"):
            cols[f"{k}{tg}"] = np.ones(n)
        cols[f"c{tg}"] = np.zeros(n, np.int8)
    for k in ("off_line", "wbf", "abf", "pbf", "wfp", "er", "ffbf", "wd_bf", "rbf", "ff1", "w0", "asym_w", "asym_ang",
              "w1", "hb", "hb_use", "brk"):
        cols[k] = np.ones(n)
    cols.update(reach=np.zeros(n, int), s=12.5 + 25 * np.arange(n), flags=np.zeros(n, np.uint8))
    made = set(X1.reach_table(pd.DataFrame(cols), 1).columns)
    spec = {f.name for f in S.FIELDS if f.group == "xsec"}
    assert made == spec


def test_files_consumers_read_are_kept_or_replaced():
    kept = {f["name"] for f in S.TILE_FILES if f["kept"] == "yes"}
    national = " ".join(f["name"] for f in S.NATIONAL_FILES)
    for path in S.FILE_CONSUMERS:
        base = path.split("/")[-1]
        ok = path in S.FILES_REPLACED or (path.startswith("tiles/") and base in kept) or base in national
        assert ok, path


def test_stored_layouts_match_the_code():
    enc = P.encode_profiles(np.array([1, 1, 2]), np.array([10.0, 9.5, 3.0], np.float32), np.zeros(3, np.uint8))
    assert [c[0] for c in S.PROFILE_STORE["columns"]] == list(enc)
    st = P.profile_steps(np.r_[np.full(20, 10.0), np.full(20, 7.0)])
    assert len(st) == 1
    assert {c[0] for c in S.STEP_STORE["columns"]} - {"gid", "s0", "x_cm", "y_cm"} <= set(st[0])
    assert {"h", "kind", "len"} <= set(st[0])


def test_gid_scheme():
    ix, iy = 2 * nz.NX - 1, 2 * nz.NY - 1                       # the last 32 km tile of the national grid
    assert nz.Tile.parse(S.tile_id(10, 14)) == nz.Tile(10, 14)  # the id format parses as today's ids do
    old_max = (nz.NY * 100 + nz.NX + 1) * nz.SEG_BASE
    g = S.gid(0, 0, 0)
    assert g > old_max and S.gid(ix, iy, 999_999) < 2**62
    assert S.tile_of(S.gid(ix, iy, 12345)) == (ix, iy)
    assert (S.gid(3, 4, 7) // S.SEG_BASE) == S.tile_index(3, 4)  # rain weights split gids the same way


def test_doc_lists_every_field():
    text = DOC.read_text()
    for f in S.FIELDS:
        if f.name in S.X1_STAGE_TABLE:
            continue
        assert f"`{f.name}`" in text, f.name
    assert "`x1_{w,a,p}<s>_med`" in text

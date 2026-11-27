"""The one-pass extraction spec of the national 1 m LiDAR rebuild as data (rf-lxa.3, docs/lidar-extraction-spec.md).

Everything the national pass must produce per reach (``FIELDS``), where each value is computed (``STAGES``), the
stored raw data (``PROFILE_STORE``, ``SECTION_STORE``, ``STEP_STORE``), the files each tile and the national merge
keep (``TILE_FILES``, ``NATIONAL_FILES``), the reach id scheme (``gid``) and what today's consumers read
(``CONSUMERS``, ``FILE_CONSUMERS``). ``tests/test_lidarspec.py`` checks that every field and file a consumer reads is
produced, or listed in ``REPLACED`` / ``DROPPED``. ``python -m river_finder.lidarspec`` prints the field tables of the
doc.

The reference implementations of the new profile fields are in ``lidarpipe`` (``reach_profile``, ``profile_steps``,
``reach_steps``, ``encode_profiles``); the cross-section fields in ``xsec1m`` (rf-lxa.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from .xsec1m import STAGES as X1_STAGES
from .xsec1m import stage_tag

STAGES = {
    "1m": "per tile, from the 1 m DEM re-read along the reaches while the 2 m grid is in memory (rf-lxa.6)",
    "2m": "per tile, from the 2 m routing grid in memory (block-mean DEM, filled DEM, directions, accumulation)",
    "merge": "after the national merge (rf-lxa.5): needs the stitched network, the lake nodes or the final areas",
    "ext": "external data joined to the stored reach lines or points: can be redone at any time without the DEM",
    "use": "derived where it is used (scoring.prepare, maf, reachtype, xsec1m.derive, nz score); not stored",
}
DTYPES = {"bool", "int8", "int16", "int32", "int64", "uint8", "float32", "float64", "str", "category", "line"}


@dataclass(frozen=True)
class Field:
    name: str
    unit: str
    dtype: str
    stage: str
    group: str
    what: str
    today: str = "new"       # today's 8 m equivalent: "same" (same definition), a note, or "new"
    used_by: str = ""        # consumers (short)
    store: bool = False      # can be recomputed from the stored raw data (profiles, sections, lines)


def _f(name, unit, dtype, stage, group, what, today="new", used_by="", store=False) -> Field:
    return Field(name, unit, dtype, stage, group, what, today, used_by, store)


GROUPS = {
    "id": "Identity and network", "area": "Catchment area", "profile": "1 m long profile",
    "steps": "Steps and drops (waterfall and rapid candidates)", "valley": "Valley and gorge (2 m grid)",
    "catch": "Catchment statistics (local sums per tile, accumulated after the merge)",
    "lidar": "LiDAR source, lakes and water surfaces", "names": "Names, lines and REC2",
    "flood": "Flood (MAF)", "geology": "Geology", "xsec": "Cross-sections (1 m sections, rf-lxa.4)",
    "ext": "Other external data", "use": "Derived where used (not stored by the pass)",
}

FIELDS: list[Field] = [
    # --- identity and network
    _f("gid", "", "int64", "2m", "id", "reach id: tile index x 1e6 + local id (``gid``); never changed by the merge",
       "tile index x 1e6 + seg_id (64 km tiles)", "every keyed table, rain, xsec, lidar"),
    _f("tile", "", "str", "2m", "id", "32 km tile id (``tile_id``)", "64 km tile id", "merge, lidar"),
    _f("seg_id", "", "int32", "2m", "id", "local id in the tile; nz score re-numbers it per region (row order)", "same",
       "scoring, hydro, external, models"),
    _f("link", "", "int32", "2m", "id", "channel link (head or confluence to the next confluence) in the tile; "
       "profiles and steps are measured per link", "same", "steps"),
    _f("down_seg", "", "int32", "2m", "id", "downstream reach (tile-local row; region-local after nz score)", "same",
       "scoring, labels, viewer"),
    _f("down_gid", "", "int64", "merge", "id", "downstream reach after stitching across tile edges, through lake "
       "nodes (rf-lxa.5); -1 at an outlet", "stitch 24 m + REC2 relink", "nz score, models, rain"),
    _f("end_x", "m", "float64", "2m", "id", "NZTM x of the line's last vertex", "same", "merge"),
    _f("end_y", "m", "float64", "2m", "id", "NZTM y of the line's last vertex", "same", "merge"),
    _f("exits_core", "", "bool", "2m", "id", "the reach drains out of the tile's core", "same", "merge"),
    _f("inflow", "", "bool", "2m", "id", "a REC2 point inflow enters on the reach (in-tile area only)",
       "lidarpipe.segments", "area check"),
    _f("truncated", "", "bool", "merge", "id", "part of the upstream network ends at a box edge with no stitch "
       "or inflow (its area is too small)", "cell flag at the reach middle (truncated.tif)", "viewer, rain flag 16"),
    _f("outlet_kind", "", "uint8", "merge", "id", "0 not an outlet, 1 sea, 2 inland sink or lake without an "
       "outlet, 3 unresolved box edge", "relink_log.csv / unresolved_gt200.csv", "rain, QA"),
    _f("stitch", "", "uint8", "merge", "id", "how down_gid was set: 0 inside the tile, 1 stitched at a tile edge, "
       "2 through a lake node, 3 REC2 relink (fallback), 4 none", "relink_log.csv status", "rain, QA"),
    _f("lake_id", "", "int32", "merge", "id", "the national lake table's lake the reach lies in or ends in (-1)",
       used_by="merge, hydro, viewer"),
    _f("geometry", "m", "line", "2m", "id", "the reach's 2 m cell-centre path (upstream to downstream, ending on the "
       "next reach's first cell), simplified at 1 m", "8 m D8 path, a vertex every ~25 m",
       "viewer, external, aerial, lidar, labels, sections"),
    _f("region", "", "str", "merge", "id", "river-guide region of the line midpoint (national.assign_regions)", "same",
       "nz score, viewer, rain, hydro"),
    _f("vi", "", "int32", "merge", "id", "index in the region's viewer arrays (reaches of 2 km2 or more), -1 not "
       "shown", "same", "viewer scores, rain, aerial, sections"),

    # --- catchment area
    _f("area_km2", "km2", "float32", "merge", "area", "area at the reach bottom: network accumulation of local_km2 "
       "over the stitched national network, lakes as single nodes (rf-lxa.5)",
       "flow accumulation in the tile + REC2 inflows at the box edge", "everything"),
    _f("area_mid_km2", "km2", "float32", "merge", "area", "mean area along the reach: area_tile_mid_km2 x "
       "area_km2 / area_tile_km2", "in-tile mean", "ksn, melton, MAF and name matching"),
    _f("area_tile_km2", "km2", "float32", "2m", "area", "in-tile area at the reach bottom with REC2 inflows "
       "(today's method; kept as a check)", "area_km2", "area_check"),
    _f("area_tile_mid_km2", "km2", "float32", "2m", "area", "in-tile mean area along the reach body",
       "area_mid_km2", "area_mid_km2"),
    _f("local_km2", "km2", "float32", "2m", "area", "area of the box cells (core and halo) that first drain into "
       "the reach; each cell is counted once, by the tile whose core holds the reach (lidarpipe.local_areas)",
       "relink.tile_local_km2 (pixels of the core only)", "merge, relink, rain"),
    _f("area_check", "ratio", "float32", "merge", "area", "area_tile_km2 / area_km2 (flag reaches off by > 10 %)",
       used_by="QA"),

    # --- 1 m long profile
    _f("length_m", "m", "float32", "1m", "profile", "length of the 2 m line after a 20 m moving average "
       "(lidarpipe.smooth_distance): no D8 staircase", "D8 cell-step length on 8 m",
       "scoring weights, runs, labels, hydro", True),
    _f("z_top", "m", "float32", "1m", "profile", "1 m profile (running minimum along the link) at the reach's "
       "first station, NZVD2016", "filled 8 m DEM at the path's top", "runs, viewer", True),
    _f("z_bot", "m", "float32", "1m", "profile", "the same at the reach's end (the next reach's first station)",
       "filled 8 m DEM at the path's end", "runs, viewer", True),
    _f("gradient_m_per_km", "m/km", "float32", "1m", "profile", "(z_top - z_bot) / length_m",
       "filled 8 m drop / D8 length", "scoring (grad_s), labels", True),
    _f("steepest_50m_m_per_km", "m/km", "float32", "1m", "profile", "steepest 50 m of the running-minimum profile "
       "inside the reach (the reach gradient when shorter than 45 m)", "the same on the filled 8 m profile",
       "FEATURES steep50, viewer s50, external check", True),
    _f("steepest_100m_m_per_km", "m/km", "float32", "1m", "profile", "the same over 100 m (robust over "
       "flattened treads, rf-lxa.1)", used_by="ML candidate, web", store=True),
    _f("steepest_200m_m_per_km", "m/km", "float32", "1m", "profile", "the same over 200 m",
       used_by="ML candidate, web", store=True),
    _f("steep50_water", "", "bool", "1m", "profile", "the steepest 50 m lies mostly on flattened or planar water "
       "(a survey riser or interpolation, not a fall)", used_by="QA, ML", store=True),
    _f("longest_flat_m", "m", "float32", "1m", "profile", "longest stretch dropping less than 5 cm (rf-a33)",
       used_by="QA", store=True),
    _f("ksn", "m^0.9", "float32", "merge", "profile", "gradient / 1000 x (area_mid_km2 x 1e6) ^ 0.45",
       "same (in the tile)", "ksn_s, knick_ratio"),
    _f("knick_ratio", "ratio", "float32", "merge", "profile", "ksn / max(ksn of the reach below, 1)",
       "same (in the tile)", "FEATURES knick, external check"),
    _f("sinuosity", "ratio", "float32", "ext", "profile", "length_m / straight distance between the line's ends",
       used_by="ML candidate", store=True),
    _f("sinuosity_1km", "ratio", "float32", "merge", "profile", "channel length / straight distance over 1 km of "
       "main stem centred on the reach (stored lines)", used_by="ML candidate", store=True),

    # --- steps
    _f("steps_n", "", "int16", "1m", "steps", "bed steps of 1 m or more (lidarpipe.profile_steps; not risers on "
       "flattened water, not planar water)", "OSM falls_n (mapped falls only)", "ML candidate, web", True),
    _f("steps_drop_m", "m", "float32", "1m", "steps", "sum of the bed steps' heights", used_by="ML candidate",
       store=True),
    _f("step_max_m", "m", "float32", "1m", "steps", "tallest bed step", "OSM falls_max_h_m",
       "ML candidate, web", True),
    _f("step_max_s_m", "m", "float32", "1m", "steps", "position of the tallest step, m from the reach top",
       used_by="web", store=True),
    _f("steps_share", "share", "float32", "1m", "steps", "steps_drop_m / reach drop (drop >= 0.5 m)",
       used_by="ML candidate", store=True),
    _f("drops_n", "", "int16", "1m", "steps", "bed steps of 2 m or more", used_by="ML candidate, web", store=True),
    _f("falls1m_n", "", "int16", "1m", "steps", "bed steps of 5 m or more (waterfall candidates)",
       used_by="ML candidate, web, OSM check", store=True),
    _f("risers_n", "", "int16", "1m", "steps", "steps between hydro-flattened treads (survey breaklines)",
       used_by="QA", store=True),

    # --- valley and gorge (2 m)
    _f("gorge_depth_m", "m", "float32", "2m", "valley", "mean over the reach cells of the highest 2 m cell within "
       "+-150 m (square) minus the channel cell", "same on the 8 m DEM", "FEATURES gorge, runs, viewer, audit"),
    _f("valley_width_m", "m", "float32", "2m", "valley", "area of the cells within 5 m above their own channel "
       "cell (HAND on the filled 2 m DEM) per metre of reach", "same on the 8 m DEM",
       "EXTRA valley_w, reachtype, audit"),
    _f("height_to_width", "ratio", "float32", "2m", "valley", "gorge_depth_m / valley_width_m", "same",
       "FEATURES h_to_w, runs, viewer hw"),
    _f("hand_w1_m", "m", "float32", "2m", "valley", "as valley_width_m with HAND <= 1 m (proposed: cheap, needs "
       "the 2 m grid)", used_by="ML candidate"),
    _f("hand_w2_m", "m", "float32", "2m", "valley", "the same with HAND <= 2 m (proposed)", used_by="ML candidate"),
    _f("hand_w10_m", "m", "float32", "2m", "valley", "the same with HAND <= 10 m (proposed)",
       used_by="ML candidate"),
    _f("hand_w20_m", "m", "float32", "2m", "valley", "the same with HAND <= 20 m (proposed)",
       used_by="ML candidate"),

    # --- catchment statistics
    _f("local_z_mean_m", "m", "float32", "2m", "catch", "mean filled 2 m elevation of the reach's local catchment",
       used_by="head_m"),
    _f("local_z_max_m", "m", "float32", "2m", "catch", "highest 2 m cell of the local catchment", used_by="relief_m"),
    _f("local_flat_km2", "km2", "float32", "2m", "catch", "local area with slope < 4 deg (slope of the 10 m block "
       "mean, close to today's 8 m)", used_by="upstream_flat_frac"),
    _f("local_hard_km2", "km2", "float32", "2m", "catch", "local area on resistant rock (GNS 1:250k rasterised "
       "per tile)", used_by="upstream_hard_frac"),
    _f("local_lidar_km2", "km2", "float32", "2m", "catch", "local area whose 2 m cells are at least half valid 1 m "
       "cells", used_by="catch_lidar_share"),
    _f("local_lake_km2", "km2", "float32", "2m", "catch", "local area on filled lake holes or flattened lakes "
       "(proposed)", used_by="upstream_lake_frac"),
    _f("head_m", "m", "float32", "merge", "catch", "mean elevation of the whole upstream catchment (local means "
       "weighted by local_km2) minus the profile at the reach's middle", "same on 8 m (per-cell accumulation)",
       "FEATURES head, viewer"),
    _f("relief_m", "m", "float32", "merge", "catch", "highest cell of the whole upstream catchment minus the "
       "profile at the reach's middle", "same on 8 m", "melton"),
    _f("melton", "", "float32", "merge", "catch", "relief_m / sqrt(area_mid_km2 x 1e6)", "same",
       "FEATURES melton, run finder H/A, viewer mel"),
    _f("upstream_flat_frac", "share", "float32", "merge", "catch", "accumulated local_flat_km2 / area_km2",
       "same on the 8 m filled DEM", "FEATURES plateau"),
    _f("upstream_hard_frac", "share", "float32", "merge", "catch", "accumulated local_hard_km2 / area_km2", "same",
       "FEATURES hard_up"),
    _f("upstream_lake_frac", "share", "float32", "merge", "catch", "accumulated local_lake_km2 / area_km2 "
       "(proposed: lakes damp floods)", used_by="ML candidate, hydro"),
    _f("catch_lidar_share", "share", "float32", "merge", "catch", "accumulated local_lidar_km2 / area_km2",
       "lidar.py (footprints on 8 m local catchments)", "QA, card v5 strata"),
    _f("karst_up_frac", "share", "float32", "merge", "catch", "local_km2 x karst_bed accumulated / area_km2 "
       "(ml.add_geology, midpoint geology)", "same (local area from area differences)", "EXTRA karst_up"),

    # --- LiDAR source, lakes, water
    _f("dem_source", "", "category", "1m", "lidar", "lidar (>= 95 % of the stations on 1 m data), mixed, 8m "
       "(< 5 %)", "lidar.source_flag (footprints)", "QA, card v5", True),
    _f("lidar_share", "share", "float32", "1m", "lidar", "share of the reach's stations with a 1 m cell within "
       "2 m (the rest are on the 2 m grid: 8 m fallback or lake fill)", "footprint share along the line (lidar.py)",
       "QA, section domain", True),
    _f("dem_res_m", "m", "float32", "1m", "lidar", "resolution of the data the profile was measured on: 1 (lidar) "
       "or 8 (mixed, 8m)", used_by="QA, card v5", store=True),
    _f("seam_dist_m", "m", "float32", "2m", "lidar", "distance from the reach line to the nearest LiDAR / 8 m "
       "fallback seam (10 m work grid, capped at 5 km)", used_by="QA"),
    _f("lake", "", "bool", "2m", "lidar", "at least half of the reach's stations on a filled lake hole or a "
       "flattened lake of the national lake table", used_by="QA, viewer, hydro, scoring domain"),
    _f("lake_share", "share", "float32", "2m", "lidar", "share of the reach's stations on a lake", used_by="QA"),
    _f("water_flat_share", "share", "float32", "1m", "lidar", "share of the stations on hydro-flattened 1 m cells",
       used_by="QA, gradient and step reliability", store=True),
    _f("water_planar_share", "share", "float32", "1m", "lidar", "share of the stations on planar (interpolated) "
       "1 m cells", used_by="QA", store=True),
    _f("lidar_survey", "", "category", "ext", "lidar", "survey slug over most of the reach (mosaic "
       "capture-dates footprints)", "lidar_slug (reach_coverage.pkl)", "QA, web"),
    _f("lidar_year", "year", "int16", "ext", "lidar", "end year of that survey's flights", "same", "QA, web"),
    _f("lidar_year_start", "year", "int16", "ext", "lidar", "start year of that survey's flights", "same", "QA"),

    # --- names, lines, REC2
    _f("river", "", "str", "merge", "names", "REC2 name (nearest REC2 reach within 150 m, area 0.6-1.6x), else "
       "river_linz", "REC2 name only", "labels, scoring, models, hydro, viewer"),
    _f("river_rec2", "", "str", "merge", "names", "the REC2 name alone"),
    _f("river_linz", "", "str", "ext", "names", "name of the LINZ river line (LDS 103632) within 30 m over most of "
       "the reach", used_by="river"),
    _f("linz_dist_m", "m", "float32", "ext", "names", "median distance from the reach's 25 m sample points to the "
       "nearest LINZ river line, capped at 500 m", used_by="QA, rf-3ri.7 check"),
    _f("linz_section_id", "", "int64", "ext", "names", "river_section_id of that LINZ line", used_by="web"),
    _f("rec2_nzsegment", "", "int64", "merge", "names", "REC2 reach matched along the network (area ratio + line "
       "distance, rf-33a.4)", "not kept (validate.py snaps at outlets)", "validation, relink, names"),
    _f("rec2_area_km2", "km2", "float32", "ext", "names", "REC2 CUM_AREA of that reach",
       "validate.py (reference areas)", "validation"),
    _f("rec2_dist_m", "m", "float32", "ext", "names", "distance from the reach midpoint to that REC2 line",
       used_by="validation"),

    # --- flood
    _f("specific_maf", "m3/s/km2", "float32", "merge", "flood", "H&C 2018 MAF / area of the nearest REC1 reach "
       "within 400 m with area 0.4-2.5x area_mid_km2", "same (unmatched: tile median)", "maf, scoring"),
    _f("maf_m3s", "m3/s", "float32", "merge", "flood", "specific_maf x area_km2 (corrected by maf.apply when on)",
       "same", "everything"),
    _f("specific_maf_matched", "", "bool", "merge", "flood", "a REC1 reach was matched", "same", "maf refill"),
    _f("rec1_nzreach", "", "int64", "merge", "flood", "the matched REC1 reach (NZREACH), so the MAF can be redone "
       "without matching", used_by="maf"),

    # --- geology
    _f("bed_hard", "0/1", "int8", "ext", "geology", "resistant rock at the reach midpoint", "same",
       "FEATURES bed_hard, runs, viewer"),
    _f("bed_rock", "", "str", "ext", "geology", "GNS rock group at the midpoint", "same", "GIS export"),
    _f("rockclass", "", "category", "ext", "geology", "GNS rock class at the midpoint (ml.add_geology)", "same",
       "ML categorical"),
    _f("rockgroup", "", "category", "ext", "geology", "GNS rock group at the midpoint", "same", "ML categorical"),
    _f("mainrock", "", "category", "ext", "geology", "GNS main rock at the midpoint", "same", "karst_bed"),
    _f("rock_age_ma", "Ma", "float32", "ext", "geology", "minimum age of the bed rock unit", "same", "EXTRA rock_age"),
    _f("karst_bed", "0/1", "int8", "ext", "geology", "bed on limestone, marble, dolomite ...", "same",
       "EXTRA karst_bed"),

    # --- other external
    _f("d_road_m", "m", "float32", "ext", "ext", "distance to the nearest road (OSM via Overture)", "same",
       "viewer, audit (never a model input)"),
    _f("d_track_m", "m", "float32", "ext", "ext", "distance to the nearest 4WD or private road", "same", "viewer"),
    _f("d_foot_m", "m", "float32", "ext", "ext", "distance to the nearest foot track", "same", "viewer"),
    _f("falls_n", "", "int16", "ext", "ext", "OSM waterfalls snapped within 100 m", "same", "EXTRA falls, viewer"),
    _f("falls_max_h_m", "m", "float32", "ext", "ext", "tallest mapped OSM waterfall", "same", "EXTRA falls_h, viewer"),
    _f("rapids_n", "", "int16", "ext", "ext", "OSM rapids within 150 m of the midpoint", "same", "EXTRA rapids"),
    _f("hydro", "", "category", "ext", "ext", "'', diverted, regulated, reservoir (hydro.py, config/hydro.toml)",
       "same", "web, audit"),
    _f("hydro_scheme", "", "str", "ext", "ext", "the scheme that contributes most", "same", "web, audit"),
    _f("hydro_point", "", "str", "ext", "ext", "the point that contributes most", "same", "web"),
    _f("hydro_share", "share", "float32", "ext", "ext", "share behind the class", "same", "web"),
    _f("div_share", "share", "float32", "ext", "ext", "diverted share", "same", "hydro"),
    _f("reg_share", "share", "float32", "ext", "ext", "regulated share", "same", "hydro"),

    # --- derived where used
    _f("up_seg", "", "int32", "use", "use", "main upstream reach (largest area)", "same", "runs, viewer"),
    _f("down", "", "int64", "use", "use", "downstream row in the national table", "same", "models, ml, rain, hydro"),
    _f("up", "", "int64", "use", "use", "main upstream row in the national table", "same", "models, hydro"),
    _f("x", "m", "float64", "use", "use", "NZTM x of the line midpoint", "same", "models, ml, hydro, zones"),
    _f("y", "m", "float64", "use", "use", "NZTM y of the line midpoint", "same", "models, ml, hydro, zones"),
    _f("lon", "deg", "float64", "use", "use", "longitude of the midpoint", "same", "models, rain"),
    _f("lat", "deg", "float64", "use", "use", "latitude of the midpoint", "same", "models, rain"),
    _f("rnorm", "", "str", "use", "use", "normalised river name (models.norm_river)", "same", "labels, hydro"),
    _f("guide_region", "", "str", "use", "use", "region used for labels and LORO (= region)", "same", "ml, models"),
    _f("outlet", "", "int64", "use", "use", "row of the reach's sea outlet (CV folds)", "same", "ml"),
    _f("grad_s", "m/km", "float32", "use", "use", "gradient over the main upstream reach, the reach and the one "
       "below (scoring.prepare)", "same", "everything"),
    _f("ksn_s", "", "float32", "use", "use", "ksn smoothed the same way", "same", "FEATURES ksn, viewer"),
    _f("power_kw_m", "kW/m", "float32", "use", "use", "rho g MAF grad_s", "same", "FEATURES power, viewer"),
    _f("maf_raw_m3s", "m3/s", "float32", "use", "use", "MAF before maf.apply", "same", "maf"),
    _f("maf_flag", "", "str", "use", "use", "what maf.apply changed", "same", "maf"),
    _f("confinement_ratio", "ratio", "float32", "use", "use", "valley_width_m / (4 MAF^0.5) (reachtype)", "same",
       "EXTRA confine"),
    _f("reach_type", "", "category", "use", "use", "Montgomery & Buffington type (reachtype)", "same", "ml strata"),
    _f("style", "", "category", "use", "use", "paddling style (reachtype)", "same", "auc_style"),
    _f("style_p", "", "float32", "use", "use", "probability of that style", "same", "ml"),
    _f("transitional", "", "bool", "use", "use", "between two adjacent types", "same", "ml"),
    _f("label", "", "int8", "use", "use", "pooled-regression training label (nz score)", "same", "models, viewer"),
    _f("holdout", "", "bool", "use", "use", "held out of the pooled regression", "same", "models, viewer"),
    _f("score_regression", "pct", "float32", "use", "use", "pooled regression percentile", "same", "viewer reg"),
    _f("score_signature", "pct", "float32", "use", "use", "similarity to known runs, percentile", "same",
       "viewer sig"),
    _f("score_composite", "pct", "float32", "use", "use", "composite score, percentile", "same", "runs"),
    _f("run_id", "", "int32", "use", "use", "run of the default run finder (-1 none)", "same", "runs.geojson"),
]
for _k in ("p_colluvial", "p_bedrock", "p_cascade", "p_step_pool", "p_plane_bed", "p_pool_riffle", "p_dune_ripple"):
    FIELDS.append(_f(_k, "", "float32", "use", "use", "reach-type probability (reachtype)", "same", "ml"))

# --- cross-sections (xsec1m.reach_table, reaches of 1 km2 or more; every value from the stored sections)
_X1_QA = {"x1_n": "kept sections", "x1_n_all": "all sections", "x1_drop_share": "share of sections dropped",
          "x1_edge_share": "share flagged EDGE", "x1_twin_share": "share flagged TWIN", "x1_low_share": "share LOW",
          "x1_far_share": "share FAR", "x1_flatw_frac": "kept sections on flattened water",
          "x1_planw_frac": "kept sections on planar water", "x1_nodata": "no-data share of the profiles"}
_X1_MED = {"w0": "width at the 0.1 m stage: the water surface width at capture", "b": "shape exponent w ~ h^b "
           "(keep: x1_b)", "m": "shape exponent A ~ h^m", "ff1": "form factor at 1 m", "ffbf": "form factor at "
           "bankfull (keep: x1_ffbf)", "hb_use": "bankfull stage used (5 m without a break)", "wbf": "bankfull "
           "width", "abf": "bankfull area", "pbf": "bankfull perimeter", "wd_bf": "bankfull width / stage",
           "rbf": "hydraulic radius at bankfull", "er": "entrenchment ratio", "ang": "bank angle, both sides "
           "(keep: x1_bank)", "ang_hi": "steeper bank angle", "ang_lo": "gentler bank angle", "steep": "share of the "
           "bank in > 60 deg steps (keep: x1_steep)", "steep_hi": "the steeper side's share", "asym_w": "width "
           "asymmetry at 2 m", "asym_ang": "bank-angle asymmetry", "wall_lo": "lower side's highest ground within "
           "150 m (keep: x1_wall)", "wall_hi": "higher side's", "bank_lo": "lower side's highest ground within 50 m",
           "vf": "valley floor width-to-height (keep: x1_vf)", "aoff": "distance of the channel point from the line",
           "hb": "bankfull stage (break only)"}
for _k, _w in _X1_QA.items():
    FIELDS.append(_f(_k, "", "int16" if _k in ("x1_n", "x1_n_all") else "float32", "1m", "xsec", _w,
                     "xs_n (8 m)" if _k == "x1_n" else "new", "ML, QA", True))
for _k, _w in _X1_MED.items():
    FIELDS.append(_f(f"x1_{_k}_med", "", "float32", "1m", "xsec", f"median of the kept sections: {_w}",
                     used_by="ML (keep list)" if "keep" in _w else "ML candidates", store=True))
for _k, _w in (("brk_frac", "share of sections with a bankfull break"), ("b_iqr", "spread of b along the reach"),
               ("w1_cv", "variation of the 1 m width along the reach"), ("hb_cv", "variation of the bankfull stage"),
               ("pinch_frac", "share of pinch points (narrower than half the median at 1 m)"),
               ("rough", "bed roughness: robust spread of the detrended 1 m long profile (keep)"),
               ("dropconc", "share of the drop in the steepest 10 % of the 1 m long profile (keep)"),
               ("grad1m", "gradient of the 1 m long profile through the section points"),
               ("prof_len", "length of that profile")):
    FIELDS.append(_f(f"x1_{_k}", "", "float32", "1m", "xsec", _w, used_by="ML", store=True))
X1_STAGE_TABLE: list[str] = []
for _h in X1_STAGES:
    _t = stage_tag(_h)
    _old = _h in (1, 2, 5, 10, 20)
    for _k, _w, _u in (("w", "width", "m"), ("a", "flow area", "m2"), ("p", "wetted perimeter", "m")):
        FIELDS.append(_f(f"x1_{_k}{_t}_med", _u, "float32", "1m", "xsec", f"median {_w} at the {_h:g} m stage",
                         f"xs_{_k}{_t}_med (8 m)" if _old else "new", "hydraulics after the merge, ML", True))
    FIELDS.append(_f(f"x1_w{_t}_min", "m", "float32", "1m", "xsec", f"narrowest section's width at {_h:g} m",
                     f"xs_w{_t}_min" if _old else "new", "constriction, ML", True))
    FIELDS.append(_f(f"x1_cens{_t}", "share", "float32", "1m", "xsec", f"share of sections censored at {_h:g} m",
                     f"xs_cens{_t}" if _old else "new", "QA", True))
    X1_STAGE_TABLE += [f"x1_w{_t}_med", f"x1_a{_t}_med", f"x1_p{_t}_med", f"x1_w{_t}_min", f"x1_cens{_t}"]
for _k, _w in (("h_q", "Manning flood stage"), ("w_q", "flood width"), ("v_q", "flood velocity (x1_vel, registry "
               "only)"), ("omega_q", "flood unit stream power (dropped from recipes)")):
    FIELDS.append(_f(f"x1_{_k}", "", "float32", "use", "use", f"{_w} on the median section (xsec1m.derive: MAF, "
                     "grad_s)", f"xs_{_k} (8 m)", "registry, web"))

BY_NAME = {f.name: f for f in FIELDS}

# --- what consumers read today that the pass does not produce under that name
_XS_STAGES = (1, 2, 5, 10, 20)
REPLACED: dict[str, str] = {"xs_n": "x1_n", "xs_res_m": "dem_res_m", "xs_bank_lo_med": "x1_bank_lo_med",
                            "xs_gorge_lo_med": "x1_wall_lo_med", "xs_gorge_hi_med": "x1_wall_hi_med",
                            "xs_nodata": "x1_nodata", "xs_h_q": "x1_h_q",
                            "xs_w_q": "x1_w_q", "xs_v_q": "x1_v_q", "xs_omega_q": "x1_omega_q",
                            "xs_omega10_wm2": "x1_w10_med", "relink_status": "stitch",
                            "unresolved_outlet": "outlet_kind",
                            "lidar_slug": "lidar_survey", "source": "dem_source"}
for _h in _XS_STAGES:
    _t = stage_tag(_h)
    for _k in ("w", "a", "p"):
        REPLACED[f"xs_{_k}{_t}_med"] = f"x1_{_k}{_t}_med"
    REPLACED[f"xs_w{_t}_min"] = f"x1_w{_t}_min"
    REPLACED[f"xs_cens{_t}"] = f"x1_cens{_t}"
DROPPED: dict[str, str] = {k: "8 m sections only (no consumer outside xsec.py); recomputable from the stored 1 m "
                            "sections" for k in ("xs_bank_lo_p90", "xs_bank_hi_med", "xs_gorge_lo_min", "xs_flat_frac",
                                                 "xs_a_q")}

# --- today's consumers (step 1 of rf-lxa.3): the per-reach columns each one reads. The feature builders
# (scoring.FEATURES, ml.EXTRA, the rf-lxa.4 keep list, reachtype.nz_inputs) are read from the code by
# ``consumer_fields`` so this list cannot drift from them.
CONSUMERS: dict[str, tuple[str, ...]] = {
    "scoring (prepare, label, runs)": ("gradient_m_per_km", "ksn", "length_m", "down_seg", "area_km2", "maf_m3s",
                                       "z_top", "z_bot", "steepest_50m_m_per_km", "gorge_depth_m", "melton",
                                       "height_to_width", "bed_hard", "river", "up_seg", "grad_s"),
    "nz score + nz.merge_segments": ("gid", "down_gid", "exits_core", "end_x", "end_y", "tile", "area_km2",
                                     "length_m", "geometry", "river", "grad_s", "region", "seg_id", "down_seg"),
    "models (national_table, map_section, cards)": (
        "seg_id", "gid", "down_gid", "river", "area_km2", "length_m", "score_regression", "label", "holdout",
        "grad_s", "steepest_50m_m_per_km", "ksn_s", "power_kw_m", "maf_m3s", "gorge_depth_m", "head_m",
        "upstream_flat_frac", "upstream_hard_frac", "bed_hard", "knick_ratio", "melton", "height_to_width",
        "valley_width_m", "specific_maf_matched", "maf_raw_m3s", "maf_flag", "x", "y", "lon", "lat", "down", "up",
        "rnorm", "guide_region", "vi", "region", "down_seg", "geometry"),
    "ml (ml_table, labels, strata, audit)": (
        "down", "x", "y", "area_km2", "seg_id", "gid", "maf_m3s", "style", "guide_region", "outlet", "rockclass",
        "rockgroup", "mainrock", "rock_age_ma", "karst_bed", "karst_up_frac", "falls_n", "falls_max_h_m", "rapids_n",
        "d_road_m", "hydro", "hydro_scheme", "lon", "lat", "up", "river", "region", "steepest_50m_m_per_km",
        "gorge_depth_m", "grad_s", "length_m", "valley_width_m", "confinement_ratio", "xs_n", "x1_n"),
    "ml.EXTRA xs_* builders (8 m sections)": ("xs_w10_med", "xs_w20_med", "xs_gorge_lo_med", "xs_a20_med",
                                              "xs_w20_min", "xs_omega10_wm2", "xs_v_q", "xs_omega_q"),
    "xsec.reach_table / hydraulics / derive (xsec.pkl)": tuple(
        [f"xs_{k}{stage_tag(h)}_med" for h in _XS_STAGES for k in ("w", "a", "p")]
        + [f"xs_w{stage_tag(h)}_min" for h in _XS_STAGES] + [f"xs_cens{stage_tag(h)}" for h in _XS_STAGES]
        + ["xs_n", "xs_res_m", "xs_bank_lo_med", "xs_bank_lo_p90", "xs_bank_hi_med", "xs_gorge_lo_med",
           "xs_gorge_lo_min", "xs_gorge_hi_med", "xs_flat_frac", "xs_nodata", "xs_omega10_wm2", "xs_h_q", "xs_w_q",
           "xs_a_q", "xs_v_q", "xs_omega_q", "gid", "area_km2", "geometry", "maf_m3s", "grad_s"]),
    "maf (correction)": ("area_km2", "maf_m3s", "maf_raw_m3s", "specific_maf_matched", "specific_maf", "down",
                         "down_seg", "grad_s", "power_kw_m", "gid"),
    "labels (section placement)": ("down_seg", "up_seg", "length_m", "area_km2", "grad_s", "gradient_m_per_km",
                                   "river", "geometry"),
    "hydro (hydro.pkl)": ("x", "y", "river", "rnorm", "area_km2", "down", "up", "length_m", "grad_s", "seg_id",
                          "region", "gid", "vi"),
    "external (OSM attach, waterfall check)": ("seg_id", "area_km2", "steepest_50m_m_per_km", "knick_ratio",
                                               "grad_s", "geometry", "region"),
    "lidar (coverage)": ("gid", "geometry", "down", "lidar_slug", "source", "lidar_share", "lidar_year",
                         "lidar_year_start", "catch_lidar_share"),
    "xsec1m (rf-lxa.4 study)": ("gid", "area_km2", "length_m", "geometry", "lidar_share", "maf_m3s", "grad_s"),
    "rainfall (weights, network, zones, live)": ("gid", "down", "area_km2", "region", "lon", "lat", "x", "y", "vi",
                                                 "relink_status", "unresolved_outlet"),
    "viewer_nz + viewer (map data)": ("gid", "seg_id", "down_seg", "up_seg", "area_km2", "length_m", "grad_s",
                                      "steepest_50m_m_per_km", "maf_m3s", "gorge_depth_m", "z_top", "z_bot",
                                      "bed_hard", "ksn_s", "power_kw_m", "head_m", "melton", "height_to_width",
                                      "river", "label", "holdout", "score_regression", "score_signature", "geometry",
                                      "region", "truncated"),
    "webdata (sections, hydro, aerial, access)": ("vi", "region", "x", "y", "down", "up", "rnorm", "area_km2",
                                                  "grad_s", "length_m", "guide_region", "hydro", "hydro_scheme",
                                                  "hydro_share", "geometry"),
    "nz.export_gis": ("area_km2", "geometry", "region", "run_id", "score_regression", "score_composite"),
    "mlpage": ("area_km2", "grad_s", "maf_m3s", "style"),
}

# --- files: what each tile keeps and what the national merge writes (rf-lxa.2: nothing at 1 m reaches the disk;
# the 2 m grid is not kept). mb = per full 32 km tile, compressed.
TILE_FILES: list[dict] = [
    {"name": "segments_core.gpkg", "res_m": None, "dtype": "vector", "kept": "yes", "mb": 7.5,
     "what": "core reaches: every stage-1m/2m field but the x1_* section summaries (in x1_reach.arrow), line "
             "simplified at 1 m (``FIELDS``, ``tile_columns``)",
     "used_by": "merge, lidar, maf, xsec1m"},
    {"name": "local_catchment_core.tif", "res_m": 10.0, "dtype": "int64", "kept": "yes", "mb": 1.5,
     "what": "gid of the reach each 10 m cell first drains into (centre 2 m cell), over the box cells the tile "
             "owns (core, plus halo cells draining to its core reaches), -1 elsewhere",
     "used_by": "rain weights, viewer catch.png, GIS, later catchment features"},
    {"name": "area_code.tif", "res_m": 10.0, "dtype": "uint8", "kept": "yes", "mb": 5.5,
     "what": "areacode of the in-tile 2 m accumulation, maximum per 10 m cell (keeps 1-cell channels)",
     "used_by": "viewer area.png, rain land mask, GIS"},
    {"name": "hillshade_24m.tif", "res_m": 24.0, "dtype": "uint8", "kept": "yes", "mb": 1.5,
     "what": "hillshade of the 8 m block mean, averaged to 24 m (as today)", "used_by": "viewer, GIS"},
    {"name": "profiles.arrow", "res_m": 1.0, "dtype": "Feather v2 zstd", "kept": "yes", "mb": 1.4,
     "what": "1 m long profile of every core reach (``PROFILE_STORE``)", "used_by": "later profile features"},
    {"name": "xsec.arrow", "res_m": 1.0, "dtype": "Feather v2 zstd", "kept": "yes", "mb": 30.0,
     "what": "raw cross-section profiles (``SECTION_STORE``, rf-lxa.4)", "used_by": "later section features"},
    {"name": "x1_reach.arrow", "res_m": None, "dtype": "Feather v2 zstd", "kept": "yes", "mb": 1.5,
     "what": "per-reach section summaries (xsec1m.reach_table, all x1_* fields)", "used_by": "x1 national table"},
    {"name": "steps.arrow", "res_m": 1.0, "dtype": "Feather v2 zstd", "kept": "yes", "mb": 0.25,
     "what": "one row per step (``STEP_STORE``)", "used_by": "web map layer, ML"},
    {"name": "tile.json", "res_m": None, "dtype": "json", "kept": "yes", "mb": 0.01,
     "what": "done flag, build id, counts, timings, memory, inflows, lakes filled", "used_by": "resume, viewer"},
    {"name": "hand_10m.tif", "res_m": 10.0, "dtype": "uint8", "kept": "optional", "mb": 3.5,
     "what": "HAND of the 2 m grid at the 10 m cell centre, 0.25 m steps to 63.5 m (255 = higher)",
     "used_by": "flood/valley map layers, later valley features (user decision)"},
    {"name": "dem_10m.tif", "res_m": 10.0, "dtype": "float32 LERC 1 cm", "kept": "optional", "mb": 14.0,
     "what": "block mean of the 1 m cells at 10 m (8 m fallback in the gaps)",
     "used_by": "later catchment features without re-reading the 1 m data (user decision)"},
    {"name": "dem_2m / filled / directions / accumulation", "res_m": 2.0, "dtype": "", "kept": "no", "mb": 0.0,
     "what": "in memory only (rf-lxa.2 decision)", "used_by": ""},
]
NATIONAL_FILES: list[dict] = [
    {"name": "lakes.gpkg", "what": "national lake table: polygon, LiDAR water level, outlet gid, km2, tiles "
                                   "(rf-lxa.5)", "used_by": "tiles (fill level), merge", "mb": 20},
    {"name": "network.parquet", "what": "every reach's merge fields (stage merge) keyed by gid", "used_by":
     "nz score", "mb": 60},
    {"name": "x1.arrow", "what": "all tiles' x1_reach.arrow (replaces xsec.pkl)", "used_by": "ml_table", "mb": 300},
    {"name": "steps.arrow", "what": "all tiles' steps", "used_by": "web, ML", "mb": 55},
    {"name": "regions/<slug>/segments_scored.gpkg, segments_table.pkl, ml_table.pkl, external.pkl, hydro.pkl, "
             "model.json, candidates.csv", "what": "as today (nz score, models table, ml table, external attach, "
                                                     "hydro attach), with the new columns",
     "used_by": "every consumer", "mb": 2500},
]
FILE_CONSUMERS: dict[str, tuple[str, ...]] = {
    "tiles/<t>/segments_core.gpkg": ("nz.merge_segments", "lidar.tile_coverage", "maf.matched_from_tiles",
                                     "xsec.run_tile", "xsec1m.tile_reaches"),
    "tiles/<t>/local_catchment_core.tif": ("rainfall.weights", "relink.tile_local_km2", "lidar.tile_coverage",
                                           "viewer_nz.region_files", "nz.export_gis"),
    "tiles/<t>/area_code.tif": ("viewer_nz.region_files", "rainfall.weights (land)", "nz.export_gis"),
    "tiles/<t>/hillshade_24m.tif": ("viewer_nz.region_files", "nz.export_gis"),
    "tiles/<t>/truncated_core.tif": ("viewer_nz.region_files", "nz.export_gis"),
    "tiles/<t>/tile.json": ("nz.process_tile (resume)", "viewer_nz (stats)"),
    "relink_log.csv": ("rainfall.upstream.network_from_files",),
    "unresolved_gt200.csv": ("rainfall.upstream.network_from_files",),
    "xsec.pkl": ("ml.ml_table", "ml.sync_xsec"),
    "analysis/lidar/reach_coverage.pkl": ("xsec1m.tile_reaches",),
    "<region>/area_km2.tif": ("validate.validate", "export.export_region"),
}
FILES_REPLACED: dict[str, str] = {
    "tiles/<t>/truncated_core.tif": "reach field truncated + local_catchment_core.tif (viewer_nz paints the mask)",
    "relink_log.csv": "reach fields stitch, outlet_kind (network.parquet)",
    "unresolved_gt200.csv": "reach field outlet_kind",
    "xsec.pkl": "x1.arrow",
    "analysis/lidar/reach_coverage.pkl": "reach fields lidar_share, dem_source, lidar_survey, catch_lidar_share",
    "<region>/area_km2.tif": "dropped with the reference-area pipeline (reference areas run in the national pass); "
                             "REC2 validation by rec2_nzsegment / rec2_area_km2",
}

# --- stored raw data
PROFILE_STORE = {
    "file": "tiles/<t>/profiles.arrow (Feather v2, zstd 9; one row per core reach)",
    "stations": "every 1 m of 20 m-smoothed distance along the 2 m line, from the reach top; height = lowest 1 m "
                "cell within 2 m (5 x 5 cells) of the station, else the 2 m grid at the station; the running minimum "
                "is not stored",
    "columns": [("gid", "int64", "reach id"), ("n", "int32", "stations"), ("z0_cm", "int32", "first height, cm"),
                ("dz", "list<int16>", "n - 1 height differences, cm"),
                ("w4", "list<uint8>", "2-bit station codes, four per byte: 0 bed or dry, 1 hydro-flattened water, "
                                      "2 planar water, 3 not on 1 m data (2 m grid: fallback or lake fill)")],
    "codec": "lidarpipe.encode_profiles / decode_profiles (exact to 1 cm)",
    "bytes_per_km": 830,   # dry run, rf-lxa.2 sub-area (West Coast, 422 km)
}
SECTION_STORE = {
    "file": "tiles/<t>/xsec.arrow (Feather v2, zstd 9; one row per section); rf-lxa.4 storage spec",
    "where": "every 25 m on reaches of 1 km2 or more, cast on the 2 m line with a 10 m channel search",
    "columns": [("gid", "int64", "reach id"), ("s", "float32", "station, m from the reach top"),
                ("x", "int32", "channel point, NZTM cm"), ("y", "int32", "channel point, NZTM cm"),
                ("nx", "float32", "unit normal"), ("ny", "float32", "unit normal"),
                ("z0", "float32", "channel height = water surface, m"), ("flags", "uint8", "xsec1m.FLAG_*"),
                ("dprof", "int16 x 650", "heights above z0 in cm on xsec1m.U, outward differences"),
                ("nodata", "uint8 x 82", "packed no-data bits of the 651 samples")],
    "codec": "xsec1m.encode / decode", "bytes_per_section": 620,
}
STEP_STORE = {
    "file": "tiles/<t>/steps.arrow (Feather v2, zstd 9; one row per step of 1 m or more, every kind)",
    "columns": [("gid", "int64", "reach holding the step's middle"), ("s0", "float32", "m from the reach top"),
                ("x_cm", "int64", "NZTM x of the step's middle, cm"), ("y_cm", "int64", "NZTM y, cm"),
                ("h", "float32", "height, m"), ("h_excess", "float32", "height above the local gradient, m"),
                ("len", "float32", "length, m"), ("slope_max", "float32", "steepest 2 m, m/m"),
                ("z_top", "float32", "height at the step's top, m"), ("kind", "uint8", "lidarpipe.STEP_*")],
    "definition": "lidarpipe.profile_steps on each link's profile (win 4 m, slope 0.25, excess 1 m over the 100 m "
                  "gradient, gap 3 m, height >= 1 m)", "bytes_per_step": 15,
}

# --- reach ids
TILE_M = 32_000.0
HALO_M = 3_000.0
TILE_INDEX0 = 10_000      # 1 m build tile indices start here: new gids (>= 1e10) never equal an 8 m gid (< 2.3e9)
SEG_BASE = 1_000_000


def tile_id(ix: int, iy: int) -> str:
    """Id of the 32 km tile at column ``ix``, row ``iy`` of the national grid (``nz.X0``, ``nz.Y0``): ``q10y14`` is
    a quarter of the 64 km tile ``x05y07``; ``nz.Tile.parse`` reads it as (10, 14)."""
    return f"q{ix:02d}y{iy:02d}"


def tile_index(ix: int, iy: int) -> int:
    return TILE_INDEX0 + iy * 100 + ix


def gid(ix: int, iy: int, local: int) -> int:
    if not 0 <= local < SEG_BASE:
        raise ValueError("local reach id out of range")
    return tile_index(ix, iy) * SEG_BASE + local


def tile_of(g: int) -> tuple[int, int]:
    k = g // SEG_BASE - TILE_INDEX0
    return k % 100, k // 100


def tile_columns() -> dict[str, list[str]]:
    """The reach columns the tile job (rf-lxa.6, ``nz1m.py``) writes: ``segments_core.gpkg`` holds every stage
    ``1m`` / ``2m`` field except the cross-section summaries (the line is its geometry), ``x1_reach.arrow`` holds
    ``gid`` and every ``x1_*`` field of stage ``1m`` (one row per core reach; NaN, x1_n = 0, without sections)."""
    seg = [f.name for f in FIELDS if f.stage in ("1m", "2m") and f.group != "xsec" and f.name != "geometry"]
    x1 = ["gid"] + [f.name for f in FIELDS if f.stage == "1m" and f.group == "xsec"]
    return {"segments_core.gpkg": seg, "x1_reach.arrow": x1}


# --- checks and the doc tables
def consumer_fields() -> dict[str, set[str]]:
    """``CONSUMERS`` plus the columns the feature builders read, found by running them on a recording frame."""
    import numpy as np
    import pandas as pd

    from . import ml, reachtype, scoring, xsec1mstudy

    class Rec(dict):
        def __init__(self):
            super().__init__()
            self.seen = set()

        def __getitem__(self, k):
            self.seen.add(k)
            return pd.Series(np.ones(3))

        def __contains__(self, k):
            return True

    def reads(fn) -> set[str]:
        r = Rec()
        fn(r)
        return r.seen

    out = {k: set(v) for k, v in CONSUMERS.items()}
    out["scoring.FEATURES (cards v1-v4, logit13)"] = set().union(*(reads(v[0]) for v in scoring.FEATURES.values()))
    out["ml.EXTRA"] = set().union(*(reads(v[0]) for v in ml.EXTRA.values()))
    keep = ("x1_w10", "x1_w20", "x1_wall", "x1_vf", "x1_b", "x1_ffbf", "x1_bank", "x1_steep", "x1_rough",
            "x1_dropconc", "x1_vel")
    out["rf-lxa.4 keep list (xsec1mstudy.FEATURES1M)"] = set().union(
        *(reads(xsec1mstudy.FEATURES1M[k][0]) for k in keep))
    out["reachtype.nz_inputs"] = reads(reachtype.nz_inputs)
    return out


def unmet() -> dict[str, list[str]]:
    """Per consumer, the fields it reads that are neither produced nor in ``REPLACED`` / ``DROPPED`` (empty = ok)."""
    have = set(BY_NAME) | set(REPLACED) | set(DROPPED)
    return {k: sorted(v - have) for k, v in consumer_fields().items() if v - have}


def markdown(groups=None) -> str:
    """The field tables of ``docs/lidar-extraction-spec.md`` (the x1 stage table collapsed to one row)."""
    lines = []
    for g, title in GROUPS.items():
        if groups and g not in groups:
            continue
        rows = [f for f in FIELDS if f.group == g and f.name not in X1_STAGE_TABLE]
        lines += [f"#### {title}", "", "| field | unit | dtype | stage | definition | today (8 m) | used by |",
                  "|---|---|---|---|---|---|---|"]
        for f in rows:
            st = f.stage + (" (store)" if f.store else "")
            lines.append(f"| `{f.name}` | {f.unit} | {f.dtype} | {st} | {f.what} | {f.today} | {f.used_by} |")
        if g == "xsec":
            tags = ", ".join(stage_tag(h) for h in X1_STAGES)
            lines.append(f"| `x1_{{w,a,p}}<s>_med`, `x1_w<s>_min`, `x1_cens<s>` | m, m2, m, m, share | float32 | 1m "
                         f"(store) | stage table: median width, area, perimeter, narrowest width and censored share "
                         f"at the 15 stages <s> = {tags} | `xs_*` at 1, 2, 5, 10, 20 m (8 m) | hydraulics after the "
                         f"merge, ML |")
        lines.append("")
    return "\n".join(lines)


def counts() -> dict:
    """Number of fields per stage (the x1 stage table counted field by field)."""
    out: dict[str, int] = {}
    for f in FIELDS:
        out[f.stage] = out.get(f.stage, 0) + 1
    return out


if __name__ == "__main__":
    print(markdown())
    print(counts())

"""GIS export: Cloud-Optimised GeoTIFFs with QGIS styles, plus a GeoPackage of vectors.

QGIS loads ``<name>.qml`` automatically when it sits beside ``<name>.tif``, so dragging the
folder's files into QGIS gives the styled heat map without a project file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import geopandas as gpd
import rasterio.shutil
from shapely.geometry import Point, shape

from .regions import Region

RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STOPS = [0.1, 0.3, 1, 3, 10, 100, 1000]  # km2


def _area_qml() -> str:
    items = ['<item alpha="0" value="0" label="0" color="#cde2fb"/>',
             '<item alpha="0" value="0.05" label="0.05" color="#cde2fb"/>']
    for i, (v, c) in enumerate(zip(STOPS, RAMP, strict=True)):
        alpha = int(150 + 105 * i / (len(STOPS) - 1))
        items.append(f'<item alpha="{alpha}" value="{v}" label="{v} km²" color="{c}"/>')
    return f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0" styleCategories="AllStyleCategories">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="1" alphaBand="-1"
                    classificationMin="0" classificationMax="1000" nodataColor="">
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0"
                         minimumValue="0" maximumValue="1000" labelPrecision="2">
          {chr(10).join('          ' + s for s in items).strip()}
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation saturation="0" grayscaleMode="0" colorizeOn="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
"""


def export_region(region: Region) -> Path:
    d = region.data_dir()
    out = d / "gis"
    out.mkdir(exist_ok=True)
    for name in ("area_km2", "hillshade", "truncated", "fill_depth_m", "dem_8m"):
        src = d / f"{name}.tif"
        if src.exists():
            rasterio.shutil.copy(src, out / f"{name}.tif", driver="COG", compress="DEFLATE")
    (out / "area_km2.qml").write_text(_area_qml())

    gpkg = out / f"{region.name}.gpkg"
    gpkg.unlink(missing_ok=True)
    rec2 = d / "rec2_nztm.geojson"
    if rec2.exists():
        feats = json.loads(rec2.read_text())["features"]
        gdf = gpd.GeoDataFrame([f["properties"] for f in feats],
                               geometry=[shape(f["geometry"]) for f in feats], crs="EPSG:2193")
        gdf.to_file(gpkg, layer="rec2_reaches", driver="GPKG")
    runs = gpd.GeoDataFrame(
        [{"river": r.river, "status": r.status, "rec2_cum_area_km2": r.rec2_cum_area_km2} for r in region.known_runs],
        geometry=[Point(*r.outlet_wgs84) for r in region.known_runs], crs="EPSG:4326").to_crs("EPSG:2193")
    runs.to_file(gpkg, layer="reference_runs", driver="GPKG")
    for extra in ("validation_rec2.csv", "validation_summary.json", "routing_stats.json"):
        if (d / extra).exists():
            shutil.copy(d / extra, out / extra)
    (out / "README.txt").write_text(
        f"river-finder GIS export: {region.name}\n"
        "All layers are in NZTM2000 (EPSG:2193).\n\n"
        "area_km2.tif      upstream catchment area per cell (km2); styled by area_km2.qml\n"
        "hillshade.tif     hillshade from the DEM\n"
        "truncated.tif     1 = the cell's catchment reaches the edge of the processed box\n"
        "fill_depth_m.tif  depth of depression filling (m)\n"
        "dem_8m.tif        LINZ 8 m DEM mosaic used for routing\n"
        f"{region.name}.gpkg  layers rec2_reaches (NIWA REC2) and reference_runs\n")
    return out

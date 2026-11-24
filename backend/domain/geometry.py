"""Coordinate transforms shared by analysis and provider adapters."""
from pyproj import Transformer
from shapely.ops import transform


METRIC_CRS = "EPSG:3035"  # Equal-area metric CRS for mainland France and Corsica.


TO_METRIC = Transformer.from_crs(4326, METRIC_CRS, always_xy=True).transform


TO_GEO = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True).transform


def metric(geometry):
    return transform(TO_METRIC, geometry)


def geographic(geometry):
    return transform(TO_GEO, geometry)


def point_identity(point):
    return round(point.x, 2), round(point.y, 2)

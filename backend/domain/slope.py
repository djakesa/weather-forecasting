"""Estimate the steepest sampled slope in each observed parcel."""
import math

import numpy as np
from shapely import STRtree, box


GRID_STEP_M = 50
MAX_SLOPE_PERCENT = 15


def parcel_slopes(parcels, area, elevations):
    if not parcels:
        return []
    west, south, east, north = area.bounds
    step = GRID_STEP_M
    x = np.arange(math.floor(west / step), math.ceil(east / step) + 1) * step
    y = np.arange(math.floor(south / step), math.ceil(north / step) + 1) * step
    grid_x, grid_y = np.meshgrid(x, y)
    height = np.asarray(elevations(grid_x.ravel(), grid_y.ravel())).reshape(grid_x.shape)
    # Each cell is split into two triangles. The gradient of each triangle
    # combines the east-west and north-south grades, including diagonal slopes.
    eastward = np.diff(height, axis=1)
    northward = np.diff(height, axis=0)
    slope = 100 / step * np.maximum(
        np.hypot(eastward[:-1], northward[:, :-1]),
        np.hypot(eastward[1:], northward[:, 1:]),
    )
    cells = box(grid_x[:-1, :-1].ravel(), grid_y[:-1, :-1].ravel(),
                grid_x[1:, 1:].ravel(), grid_y[1:, 1:].ravel())
    tree = STRtree(cells)
    slopes = []
    for parcel in parcels:
        indices = tree.query(parcel, predicate="intersects")
        values = slope.ravel()[indices]
        slopes.append(float(values.max()) if values.size and np.isfinite(values).all() else None)
    return slopes

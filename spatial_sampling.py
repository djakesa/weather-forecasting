"""Bounded, deterministic scattered weather samples, without a weather grid."""
import math
import pandas as pd
from shapely.geometry import Point
from shapely.ops import nearest_points


def scattered_points(area, limit=16, separation_m=0):
    if area.is_empty or limit <= 0:
        return []
    x0,y0,x1,y1=area.bounds
    points=[area.representative_point()]
    def radical(n,base):
        value,step=0.,1./base
        while n:
            value += (n % base)*step
            n//=base
            step/=base
        return value
    candidates=[]
    for i in range(1, max(256,limit*64)):
        p=Point(x0+(x1-x0)*radical(i,2), y0+(y1-y0)*radical(i,3))
        if area.covers(p):
            candidates.append(p)
    while candidates and len(points)<limit:
        best=max(candidates,key=lambda p:min(p.distance(s) for s in points))
        candidates.remove(best)
        if min(best.distance(p) for p in points)<max(.01,separation_m):
            break
        points.append(best)
    return points


def sample_solar(area,start,end,tilt,pr,progress,*,evaluator,point_budget=16):
    # Model-aware spacing avoids spending quota on many near-identical cells.
    spacing=2500 if evaluator.model == "meteofrance_arome_france" else 9000
    points=scattered_points(area,min(point_budget,evaluator.remaining),spacing)
    if not points:
        raise ValueError("Aucun point météo dans la zone.")
    progress(f"Échantillonnage réparti : {len(points)} points, sans grille ni affinage.")
    frame=evaluator(points).copy()
    frame["sampling_method"]="échantillons répartis"
    return frame,frame.loc[frame["yield"].idxmax()].to_dict()


def network_proxy_candidates(area,origin,assets,records,limit=32):
    """Move towards network geometry, reusing irradiance; no weather calls."""
    candidates=[]
    for asset in assets:
        if asset.get("planning_eligible") is False or asset.get('voltage_selection_compatible') is False or asset.get("kind") not in {"transformer","substation","line","minor_line","cable"}:
            continue
        clipped=asset["geometry"].intersection(area)
        point=nearest_points(origin,clipped)[1] if not clipped.is_empty else nearest_points(area,asset["geometry"])[0]
        if area.covers(point):
            candidates.append(point)
    candidates=sorted({(round(p.x,2),round(p.y,2)):p for p in candidates}.values(),key=origin.distance)[:limit]
    rows=[]
    for point in candidates:
        nearest=min(records,key=lambda r:point.distance(Point(r["x"],r["y"])))
        row={**nearest,"x":point.x,"y":point.y,"distance_meteo_proxy_m":point.distance(Point(nearest["x"],nearest["y"]))}
        rows.append(row)
    return rows

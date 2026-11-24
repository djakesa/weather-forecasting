"""Bounded OpenInfraMap power-vector-tile reader, with persistent cache.

Published MVT voltage attributes are kV; OSM ratings remain VA/unit strings.
Geometry is a tile-clipped/generalised representation, never a full OSM trace.
"""
import math
import time
import json
import tempfile
import re
from pathlib import Path

import mapbox_vector_tile
import requests
from shapely.geometry import shape
from shapely.ops import transform,unary_union
import france_pv as core
from electrical_data import rating_kva,current_amperes

TILE_URL="https://openinframap.org/map/power/{z}/{x}/{y}.pbf"
ZOOM=14 # Transformers are only published from this zoom.


def tile_indices(area,zoom=ZOOM,maximum=64):
    w,s,e,n=core.geographic(area).bounds
    count=2**zoom
    def xy(lon,lat):
        return int((lon+180)/360*count),int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*count)
    x0,y0=xy(w,n)
    x1,y1=xy(e,s)
    if (x1-x0+1)*(y1-y0+1)>maximum:
        raise ValueError(f"OpenInfraMap : plus de {maximum} tuiles à zoom 14 ; réduire le rayon réseau. Aucun inventaire incomplet n'est validé.")
    return [(zoom,x,y) for x in range(x0,x1+1) for y in range(y0,y1+1)]


def decode_tile(data,z,x,y):
    decoded=mapbox_vector_tile.decode(data,default_options={"y_coord_down":True})
    result=[]
    kinds={"power_line":"line","power_substation":"substation","power_substation_point":"substation","power_transformer":"transformer"}
    for layer_name,layer in decoded.items():
        if layer_name not in kinds:
            continue
        extent=layer.get("extent",4096)
        def geographic(u,v,zcoord=None):
            lon=(x+u/extent)/2**z*360-180
            lat=math.degrees(math.atan(math.sinh(math.pi*(1-2*(y+v/extent)/2**z))))
            return lon,lat
        for feature in layer.get("features",[]):
            p=feature.get("properties",{})
            geometry=core.metric(transform(geographic,shape(feature["geometry"])))
            rawid=int(feature.get("id") or p.get("osm_id") or 0)
            if rawid>=2**63:
                rawid-=2**64
            osm_type="relation" if rawid<0 else "node" if p.get("is_node") else "way"
            osm_id=abs(rawid)
            # Unknown IDs must not merge distinct objects across tile edges.
            identifier=f"OIM/{osm_type}/{osm_id}" if rawid else f"OIM/tile/{z}/{x}/{y}/{layer_name}/{len(result)}"
            def volts(key):
                value=p.get(key)
                if value is None or value=="":
                    return None
                try:
                    number=float(value)*1000
                except (TypeError,ValueError):
                    return None
                if not math.isfinite(number) or number<=0:
                    return None
                return number
            keys=sorted(key for key in p if re.fullmatch(r"voltage(?:_\d+)?",key))
            values=[v for key in keys if (v:=volts(key)) is not None]
            primary,secondary,tertiary=volts("voltage_primary"),volts("voltage_secondary"),volts("voltage_tertiary")
            nominal=";".join(f"{v:g}" for v in values) if values else str(primary) if primary else "inconnue"
            kind=kinds[layer_name]
            if kind=="line" and (p.get("type")=="cable" or p.get("location") in {"underground","underwater"}):
                kind="cable"
            elif kind=="line" and p.get("type")=="minor_line":
                kind="minor_line"
            unavailable=p.get("construction") or p.get("disused") or p.get("line") in {"busbar","bay"}
            result.append(dict(id=identifier,kind=kind,geometry=geometry,voltage=nominal,
                primary_voltage_v=primary,secondary_voltage_v=secondary,tertiary_voltage_v=tertiary,
                voltage_values_v=list(dict.fromkeys(values+[v for v in (primary,secondary,tertiary) if v is not None])),
                power_kva=rating_kva(p.get("rating")),rating_raw=p.get("rating"),current_a=current_amperes(p.get('max_current') or p.get('current')),
                current_raw=p.get('max_current') or p.get('current'),cables=p.get('cables'),wires=p.get('wires'),
                cross_section=p.get('cables:cross_section') or p.get('cable:cross_section') or p.get('cross_section'),
                source="Open Infrastructure Map / OpenStreetMap",source_url=TILE_URL.format(z=z,x=x,y=y),
                osm_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if rawid else None,
                openinfra_url=f"https://openinframap.org/#16/{core.geographic(geometry.representative_point()).y:.6f}/{core.geographic(geometry.representative_point()).x:.6f}",
                electrical_value_source="OpenInfraMap MVT (tensions publiées en kV)",
                name=p.get("name"),operator=p.get("operator"),reference=p.get("ref"),circuits=p.get("circuits"),
                frequency_hz=p.get("frequency"),conductor_material=p.get('cables:material') or p.get('cable:material') or p.get("material"),
                openinfra_properties=dict(p),planning_eligible=not bool(unavailable),
                planning_reason="jeu de barres interne" if p.get("line") in {"busbar","bay"} else "construction ou hors service" if unavailable else None,
                coordinate_precision="Géométrie découpée et simplifiée en tuile OpenInfraMap, zoom 14"))
    return result


def read_tile(tile,progress):
    z,x,y=tile
    directory=core.CACHE/"openinfra_tiles"
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/f"{z}_{x}_{y}.pbf"
    if path.exists() and time.time()-path.stat().st_mtime<86400:
        try:
            return decode_tile(path.read_bytes(),z,x,y),True
        except Exception:
            pass # Corrupt bytes are replaced; quota/cooldown files are untouched.
    cooldown=directory/"cooldown.json"
    if cooldown.exists():
        try:
            deadline=float(json.loads(cooldown.read_text())["until"])
        except (OSError,ValueError,KeyError):
            deadline=0
        if deadline-time.time()>30:
            raise ValueError(f"OpenInfraMap en cooldown, réessayer dans {math.ceil(deadline-time.time())} s.")
        if deadline>time.time():
            core.wait_until(deadline,progress,"OpenInfraMap","cooldown serveur")
    url=TILE_URL.format(z=z,x=x,y=y)
    for attempt in range(2):
        response=None
        try:
            response=requests.get(url,timeout=(8,25),headers={"User-Agent":"SolarAtlas/1.0 (local PV screening)"})
            if response.status_code==429 or response.status_code>=500:
                delay=core.retry_delay(response.headers.get("Retry-After"),5)
                cooldown.write_text(json.dumps({"until":time.time()+delay}))
                if attempt or delay>30:
                    raise ValueError(f"OpenInfraMap HTTP {response.status_code}, réessayer dans {math.ceil(delay)} s.")
                core.wait_until(time.time()+delay,progress,"OpenInfraMap","serveur occupé")
                continue
            response.raise_for_status()
            if len(response.content)>8_000_000:
                raise ValueError("OpenInfraMap : tuile trop volumineuse.")
            try:
                records=decode_tile(response.content,z,x,y)
            except Exception as exc:
                raise ValueError("OpenInfraMap : tuile vectorielle illisible, non mise en cache.") from exc
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=directory,delete=False) as output:
                    temporary=Path(output.name)
                    output.write(response.content)
                temporary.replace(path)
            except OSError:
                progress("Cache OpenInfraMap non inscriptible ; données reçues conservées.")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return records,False
        except requests.RequestException as exc:
            if attempt:
                raise ValueError(f"OpenInfraMap : {type(exc).__name__}, après deux essais.") from exc
            core.wait_until(time.time()+2,progress,"OpenInfraMap","connexion interrompue")
        finally:
            if response is not None:
                response.close()


def network_assets(area,progress=lambda _:None):
    tiles=tile_indices(area)
    grouped={}
    hits=0
    for index,tile in enumerate(tiles):
        progress(f"OpenInfraMap : tuile {index+1}/{len(tiles)}…")
        records,cached=read_tile(tile,progress)
        hits+=int(cached)
        for asset in records:
            if asset["geometry"].intersects(area):
                grouped.setdefault(asset["id"],[]).append(asset)
    result=[]
    for records in grouped.values():
        # Polygon representations take precedence over centroids of that site.
        geometries=[r["geometry"] for r in records]
        polygons=[g for g in geometries if g.geom_type in {"Polygon","MultiPolygon"}]
        asset=dict(records[0])
        # Tile fragments may expose different attributes: preserve every published value.
        for record in records[1:]:
            for key,value in record.items():
                if asset.get(key) is None and value is not None:
                    asset[key]=value
        values=list(dict.fromkeys(v for r in records for v in r.get('voltage_values_v',[])))
        if values:
            asset['voltage_values_v']=values
        nominal=list(dict.fromkeys(v for r in records for v in core.osm_voltage_values(r.get('voltage'))))
        if nominal:
            asset['voltage']=';'.join(f'{v:g}' for v in nominal)
        asset["geometry"]=unary_union(polygons or geometries)
        result.append(asset)
    progress(f"OpenInfraMap : {len(result)} ouvrages ; {hits}/{len(tiles)} tuiles réutilisées du cache.")
    return result

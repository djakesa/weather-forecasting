"""Explicit JSON/GeoJSON contract; geometry never leaks as Python objects."""
import math
from datetime import datetime,timezone
import numpy as np
from shapely.geometry import mapping,LineString,Point
from france_pv import geographic
from electrical_data import voltage_label
from map_ui import irradiance_raster
from branca.colormap import linear
from branca.utilities import write_png
from folium.utilities import mercator_transform


def clean(value):
    if isinstance(value,dict):
        return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [clean(v) for v in value]
    if isinstance(value,np.generic):
        return clean(value.item())
    if isinstance(value,float) and not math.isfinite(value):
        return None
    return value


def collection(features):
    return dict(type="FeatureCollection",features=features)


def feature(geometry,properties):
    return dict(type="Feature",geometry=mapping(geographic(geometry)),properties=clean(properties))


def asset_features(assets):
    return collection([feature(asset["geometry"],{**{k:v for k,v in asset.items() if k!="geometry"},"voltage_label":voltage_label(asset)}) for asset in assets])


def serialize_result(result,config,project_id,directory):
    scenarios=[]
    for index,scenario in enumerate(result["scenarios"]):
        item={k:v for k,v in scenario.items() if k not in {"point","link"}}
        p=geographic(scenario["point"])
        item["location"]=[p.y,p.x]
        link=scenario.get("link")
        if index==2:
            link=result["collector"].get("target")
        item["connection"]=None if link is None else dict(id=link["id"],source=link.get("source"),distance_m=link["distance_m"],voltage_label=voltage_label(link),selected_voltage_v=link.get('voltage_selection_matched_v',[]))
        if link:
            item['connection'].update({key:link.get(key) for key in ('current_a','current_raw','circuits','cables','wires','conductor_material','cross_section','power_kva','source_url','osm_url')})
        item["route"]=feature(LineString([scenario["point"],link["target"]]),dict(scenario=index)) if link and index<2 else None
        scenarios.append(item)
    samples=collection([feature(Point(row["x"],row["y"]),{k:v for k,v in row.items() if k not in {"x","y"}}) for row in result["samples"].to_dict("records")])
    raster=None
    try:
        start,end=result["period"]
        from datetime import date
        hours=(date(end+1,1,1)-date(start,1,1)).days*24/(end-start+1)
        values,mask,bounds,means=irradiance_raster(result["area"],result["samples"],result["performance_ratio"],mean_year_hours=hours,cache=True)
        low=20*math.floor(float(means.min())/20)
        high=max(low+20,20*math.ceil(float(means.max())/20))
        scale=linear.YlOrRd_09.scale(low,high)
        rgba=np.empty(values.shape+(4,))
        for channel in range(3):
            rgba[:,:,channel]=np.interp(values,scale.index,[c[channel] for c in scale.colors])
        rgba[:,:,3]=mask.astype(float)
        projected=mercator_transform(rgba,[bounds[0][0],bounds[1][0]],origin="upper")
        directory.mkdir(parents=True,exist_ok=True)
        (directory/"irradiance.png").write_bytes(write_png(projected,origin="upper"))
        raster=dict(url=f"/api/projects/{project_id}/irradiance.png",bounds=bounds,min=low,max=high,mean=float(np.mean(means)),unit="W/m²")
    except (OSError,ValueError) as exc:
        result["warnings"].append(f"Rendu solaire indisponible : {exc}. Données numériques conservées.")
    parcels=[{k:v for k,v in row.items() if k not in {"geometry","footprint","link","land_price"}} for row in result["parcels"]]
    plan=result["collector"]
    collector={k:v for k,v in plan.items() if k not in {"features","target","hub"}}
    return clean(dict(id=project_id,kind="analysis",name=config.name,created_at=datetime.now(timezone.utc).isoformat(),
        config=config.model_dump(exclude={"inventory_csv","cadastral_geojson","legal_areas_geojson","rpg_geojson"}),
        zone=feature(result["area"],{}),samples=samples,network=asset_features(result["assets"]),
        cadastre=collection(result["cadastral_features"]),pv=collection(result["footprints"]),
        routes=collection(result["connection_features"]),scenarios=scenarios,parcels=parcels,
        raster=raster,collector=collector,warnings=result["warnings"],
        summary=dict(surface=result["surface"],kwp=result["kwp"],placed_m2=result["surface"]-result["missing"] if result["missing"] is not None else None,
            missing_m2=result["missing"],weather_model=result["weather_model"],weather_points=result["weather_points"],
            weather_cache_hits=result["weather_cache_hits"],weather_db_hits=result.get('weather_db_hits',0),weather_nearby_hits=result.get('weather_nearby_hits',0),weather_api_points=result.get('weather_api_points',0),network_count=len(result["assets"]),terrain_country=result["terrain_country"],compatible_network_count=sum(bool(a.get('voltage_selection_compatible')) and a.get('planning_eligible') is not False for a in result['assets'])),
        monthly_irradiance=result["monthly_irradiance"],land_reference=result["land_reference"],assumptions=result["assumptions"],
        regulatory={k:v for k,v in (result.get('regulatory') or {}).items() if k not in {'exclusions','legal_coverage'}},site_comparison=result.get('site_comparison',[]),grid_constraints=result.get('grid_constraints',{}),
        limitations=["Comparaison de secteurs échantillonnés, pas une garantie d’optimum global ; autorisations, disponibilité foncière et capacité d’injection à confirmer.","Couleur : moyenne GTI horaire interpolée, nuits comprises ; résolution native du modèle conservée."]))

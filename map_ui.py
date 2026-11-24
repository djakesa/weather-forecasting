"""Shared aerial basemaps, browser GPS and readable electrical layers."""
from html import escape

import folium
from branca.element import MacroElement, Element
from jinja2 import Template
from folium.plugins import Fullscreen, LocateControl

from france_pv import geographic, EQUIPMENT_LABELS
from shapely.geometry import Point, Polygon, mapping
from branca.colormap import linear
import math
import numpy as np
import hashlib
import tempfile
from pathlib import Path
from shapely import intersects_xy
from france_pv import TO_METRIC
from electrical_data import voltage_label


def mean_irradiance_values(samples,pr=.8,mean_year_hours=8760.):
    if "irradiance_mean_w_m2" in samples.columns:
        return samples["irradiance_mean_w_m2"].to_numpy(dtype=float)
    energy=samples["irradiance_kwh_m2"].to_numpy(dtype=float) if "irradiance_kwh_m2" in samples.columns else samples["yield"].to_numpy(dtype=float)/pr
    return energy*1000/mean_year_hours


def irradiance_raster(area,samples,pr=.8,size=160,mean_year_hours=8760.,cache=False):
    """Regular display pixels, IDW of temporal means; no extra API requests."""
    west,south,east,north=geographic(area).bounds
    lon,lat=np.meshgrid(np.linspace(west,east,size),np.linspace(north,south,size))
    x,y=TO_METRIC(lon,lat)
    records=samples.drop_duplicates(subset=["x","y"])
    means=mean_irradiance_values(records,pr,mean_year_hours)
    if not np.all(np.isfinite(means)) or np.any(means<0):
        raise ValueError("Moyennes d'irradiance invalides.")
    path=None
    if cache:
        import france_pv as core
        key=hashlib.sha256(b"raster-idw-v2"+area.wkb+records[["x","y"]].to_numpy(dtype=float).tobytes()+means.tobytes()+str(size).encode()).hexdigest()
        path=core.CACHE/"weather_renders"/(key+".npz")
        try:
            with np.load(path,allow_pickle=False) as archive:
                cached_values,cached_mask=archive["values"],archive["mask"]
                if cached_values.shape==(size,size) and cached_mask.shape==(size,size) and np.all(np.isfinite(cached_values)):
                    return cached_values,cached_mask,[[south,west],[north,east]],means
        except Exception:
            pass
    numerator=np.zeros_like(x)
    denominator=np.zeros_like(x)
    for row,value in zip(records.to_dict("records"),means):
        distance2=(x-row["x"])**2+(y-row["y"])**2
        weight=1/np.maximum(distance2,.01)
        numerator+=weight*value
        denominator+=weight
    values=numerator/denominator
    mask=intersects_xy(area,x,y)
    if path is not None:
        temporary=None
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
                temporary=Path(output.name)
                np.savez_compressed(output,values=values,mask=mask)
            temporary.replace(path)
        except OSError:
            pass # Rendering still works when caching is unavailable.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return values,mask,[[south,west],[north,east]],means


def irradiance_cells(area, samples, pr=.8):
    """Legacy partition helper; the active map uses irradiance_raster instead."""
    records=samples.drop_duplicates(subset=["x","y"]).to_dict("records")
    span=max(area.bounds[2]-area.bounds[0],area.bounds[3]-area.bounds[1],1)*8
    features=[]
    for i,row in enumerate(records):
        region=area
        for j,other in enumerate(records):
            if i==j:
                continue
            dx,dy=row["x"]-other["x"],row["y"]-other["y"]
            length=(dx*dx+dy*dy)**.5
            nx,ny=dx/length,dy/length
            tx,ty=-ny,nx
            mx,my=(row["x"]+other["x"])/2,(row["y"]+other["y"])/2
            clip=Polygon([(mx+tx*span,my+ty*span),(mx-tx*span,my-ty*span),
                          (mx-tx*span+nx*span,my-ty*span+ny*span),(mx+tx*span+nx*span,my+ty*span+ny*span)])
            region=region.intersection(clip)
        if not region.is_empty:
            features.append(dict(type="Feature",geometry=mapping(geographic(region)),properties={
                "irradiance_kwh_m2_an":row.get("irradiance_kwh_m2",row["yield"]/pr),
                "statut":"Aplat du point échantillonné le plus proche ; approximation spatiale, sans mesure locale"}))
    return features


def add_irradiance_layer(map_object,area,samples,pr=.8,show=True,mean_year_hours=8760.):
    if samples.empty:
        return
    values,mask,bounds,means=irradiance_raster(area,samples,pr,mean_year_hours=mean_year_hours,cache=True)
    lower=20*math.floor(float(means.min())/20)
    upper=max(lower+20,20*math.ceil(float(means.max())/20))
    scale=linear.YlOrRd_09.scale(lower,upper)
    scale.caption="Irradiance GTI moyenne (W/m²) · interpolation des moyennes horaires, nuits comprises"
    rgba=np.empty(values.shape+(4,))
    for channel in range(3):
        rgba[:,:,channel]=np.interp(values,scale.index,[c[channel] for c in scale.colors])
    rgba[:,:,3]=mask.astype(float)
    group=folium.FeatureGroup(name="Irradiance GTI moyenne",show=show)
    folium.raster_layers.ImageOverlay(rgba,bounds=bounds,opacity=.75,mercator_project=True,
        origin="upper",pixelated=False,interactive=False).add_to(group)
    group.add_to(map_object)
    if show:
        scale.add_to(map_object)
    points=folium.FeatureGroup(name="Points météo effectivement échantillonnés",show=False)
    for row,value in zip(samples.to_dict("records"),mean_irradiance_values(samples,pr,mean_year_hours)):
        point=geographic(Point(row["x"],row["y"]))
        folium.CircleMarker([point.y,point.x],radius=3,color="#334155",fill=True,tooltip=f"Moyenne GTI : {value:.1f} W/m²").add_to(points)
    points.add_to(map_object)

ORTHO_TILES = ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
               "&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&FORMAT=image/jpeg"
               "&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}")
class GPSSelection(MacroElement):
    _template = Template("""{% macro script(this, kwargs) %}
    {{this._parent.get_name()}}.on('locationfound', function(e) {
        this.fire('click', {latlng: e.latlng});
    });
    {% endmacro %}""")


def base_map(latitude, longitude, zoom=12, select_gps=False, country="France"):
    result = folium.Map(location=[latitude, longitude], tiles=None, zoom_start=zoom, control_scale=True)
    folium.TileLayer("OpenStreetMap",name="Fond clair",show=True,class_name="quiet-basemap").add_to(result)
    result.get_root().header.add_child(Element("<style>.quiet-basemap{filter:grayscale(1);opacity:.65}</style>"))
    folium.TileLayer(ORTHO_TILES,name='Vue aérienne IGN',attr='© IGN · Géoplateforme',max_zoom=21,max_native_zoom=19,show=False).add_to(result)
    folium.TileLayer("OpenStreetMap", name="Plan des routes", show=False).add_to(result)
    Fullscreen(title="Plein écran", title_cancel="Quitter le plein écran").add_to(result)
    LocateControl(auto_start=False, strings={"title": "Utiliser ma position GPS"},
                  locateOptions={"enableHighAccuracy": True, "maxZoom": 15}).add_to(result)
    if select_gps:
        GPSSelection().add_to(result)
    return result


def asset_popup(asset):
    fields = [("Ouvrage", asset["id"]), ("Type", EQUIPMENT_LABELS.get(asset["kind"], asset["kind"])), ("Source", asset.get("source", "OSM")),
              ("Tension publiée",voltage_label(asset)),
              ("Tension de conception (kV)",asset["design_voltage_v"]/1000 if asset.get("design_voltage_v") is not None else None),
              ("Catégorie", asset.get("voltage_class", "non publiée")),
              ("Tension nominale (V)", asset.get("voltage", "inconnue")),
              ("Tensions OSM (kV)", " ; ".join(f"{v/1000:g}" for v in dict.fromkeys(asset.get("voltage_values_v",[]))) or None),
              ("Tension primaire (kV)", asset["primary_voltage_v"]/1000 if asset.get("primary_voltage_v") is not None else None),
              ("Tension secondaire (kV)", asset["secondary_voltage_v"]/1000 if asset.get("secondary_voltage_v") is not None else None),
              ("Libellé tension source", asset.get("voltage_raw") or asset.get("voltage", "inconnue")),
              ("Puissance assignée (kVA)", asset.get("power_kva") or "non publiée"),
              ("Tension secondaire (V)", asset.get("secondary_voltage_v") or "non publiée"),
              ("Provenance électrique", asset.get("electrical_value_source", asset.get("source", "OSM"))),
              ("Intensité (source)", asset.get("current_a") or "non publiée"),
              ("Génération disponible (MW)", asset.get("available_generation_mw") if asset.get("available_generation_mw") is not None else "non publiée"),
              ("Génération occupée (MW)", asset.get("occupied_generation_mw")),
              ("Génération en attente (MW)", asset.get("pending_generation_mw")),
              ("Publication", asset.get("publication_date")), ("Nom", asset.get("name")),
              ("Gestionnaire", asset.get("operator")), ("Circuits", asset.get("circuits")),
              ("Câbles", asset.get("cables")), ("Conducteurs par faisceau", asset.get("wires")),
              ("Fréquence publiée (Hz)", asset.get("frequency_hz")),
              ("Commentaires gestionnaire", asset.get("network_comments")),
              ("Précision géographique", asset.get("coordinate_precision")),
              ("Publication source", asset.get("source_url")),
              ("Objet OSM", asset.get("osm_url")),
              ("Accès", "Valeurs indicatives non cumulables ; étude de raccordement nécessaire")]
    main={"Type","Nom","Tension publiée","Tensions OSM (kV)","Tension primaire (kV)","Tension secondaire (kV)","Puissance assignée (kVA)","Génération disponible (MW)","Source"}
    def table(rows):
        return "<table>"+"".join(f"<tr><th style='text-align:left;padding:4px'>{escape(k)}</th><td style='padding:4px'>{escape(str(v))}</td></tr>" for k,v in rows if v is not None)+"</table>"
    return table([(k,v) for k,v in fields if k in main])+"<details><summary>Détails et provenance</summary>"+table([(k,v) for k,v in fields if k not in main])+"</details>"


def add_network_layers(map_object, assets, show=True):
    groups = {"post": folium.FeatureGroup(name="Postes et transformateurs",show=show),
              "line": folium.FeatureGroup(name="Lignes aériennes",show=show),
              "cable": folium.FeatureGroup(name="Liaisons souterraines",show=show)}
    for asset in assets or []:
        kind = asset["kind"]
        if kind not in {"transformer","substation","line","minor_line","cable"}:
            continue
        group = groups["post" if kind in {"transformer", "substation"} else "cable" if kind == "cable" else "line"]
        geom = geographic(asset["geometry"])
        popup = folium.Popup(asset_popup(asset), max_width=430)
        tooltip = escape(f"{EQUIPMENT_LABELS.get(asset['kind'], asset['kind'])} · {voltage_label(asset)} · {asset.get('source', 'OSM')}")
        if kind in {"transformer", "substation"}:
            p = geom.representative_point()
            folium.CircleMarker([p.y, p.x], radius=7, color="white", weight=2,
                                fill=True, fill_color="#2563eb", fill_opacity=1,
                                tooltip=tooltip, popup=popup).add_to(group)
        else:
            color = "#f59e0b" if kind == "cable" else "#a855f7"
            folium.GeoJson(geom.__geo_interface__, tooltip=tooltip, popup=popup,
                           style_function=lambda _, color=color: {"color": color, "weight": 3}).add_to(group)
    for group in groups.values():
        group.add_to(map_object)


def add_connection_layer(map_object, features,show=True):
    if not features:
        return
    group = folium.FeatureGroup(name="Collecteur commun et raccordement unique", show=show)
    voltage_field="tension_source_libelle" if all("tension_source_libelle" in f["properties"] for f in features) else "tension_v_source"
    voltage_alias="Tension publiée" if voltage_field=="tension_source_libelle" else "Tension publiée (V)"
    folium.GeoJson({"type": "FeatureCollection", "features": features},
                   style_function=lambda f: {"color": "#06b6d4" if f["properties"].get("segment_type") == "collecteur" else "#ef4444", "weight": 4, "dashArray": "8 5"},
                   tooltip=folium.GeoJsonTooltip(fields=["parcelle", "ouvrage_id", "distance_directe_m", voltage_field],
                       aliases=["Parcelle", "Ouvrage ciblé", "Distance directe (m)", voltage_alias], localize=True),
                   popup=folium.GeoJsonPopup(fields=["parcelle", "ouvrage_id", "source", voltage_field,
                       "puissance_assignee_source", "longueur_budget_m", "liaison_basse_eur", "liaison_haute_eur",
                       "transformateur_neuf_bas_eur", "transformateur_neuf_haut_eur", "raccordement_bas_eur", "raccordement_haut_eur", "statut"],
                       aliases=["Parcelle", "Ouvrage", "Source", voltage_alias, "Puissance assignée publiée",
                       "Longueur budgétée (m)", "Liaison basse (€)", "Liaison haute (€)", "Transfo neuf bas (€)",
                       "Transfo neuf haut (€)", "Raccordement bas, aléas inclus (€)", "Raccordement haut, aléas inclus (€)", "Statut"], localize=True)).add_to(group)
    group.add_to(map_object)

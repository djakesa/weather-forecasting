"""Verified electrical values: source values, never proximity-based inference."""
import math
import re

import pandas as pd
import requests

from backend.domain.inventory import apply_verified_inventory


def current_amperes(value):
    """A published scalar only; compound ratings stay in the raw source field."""
    match=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(kA|A)?\s*',str(value),re.I)
    if not match:return None
    number=float(match[1].replace(',','.'))*(1000 if (match[2] or '').lower()=='ka' else 1)
    return number if math.isfinite(number) and number>0 else None


def exact_osm_details(osm_type,osm_id):
    """Read the tags of one exact OSM object, not another nearby asset."""
    import france_pv as core
    if osm_type not in {'node','way','relation'} or not str(osm_id).isdigit():raise ValueError('Identifiant OSM invalide')
    url=f'https://api.openstreetmap.org/api/0.6/{osm_type}/{int(osm_id)}.json'
    data=core.api(url)
    rows=[r for r in data.get('elements',[]) if r.get('type')==osm_type and r.get('id')==int(osm_id)]
    if len(rows)!=1:raise ValueError('Objet exact absent de la réponse OSM')
    row=rows[0];tags=row.get('tags') or {}
    asset=dict(osm_tags=tags,voltage=tags.get('voltage'),primary_voltage_v=core.voltage_in_volts(tags.get('voltage:primary')),
        secondary_voltage_v=core.voltage_in_volts(tags.get('voltage:secondary')),kind=tags.get('power'),
        current_a=current_amperes(tags.get('max_current') or tags.get('current')),current_raw=tags.get('max_current') or tags.get('current'),
        power_kva=rating_kva(tags.get('rating') or tags.get('rating:primary')),circuits=tags.get('circuits'),
        cables=tags.get('cables'),wires=tags.get('wires'),conductor_material=tags.get('cables:material') or tags.get('cable:material') or tags.get('material'),
        cross_section=tags.get('cables:cross_section') or tags.get('cable:cross_section') or tags.get('cross_section'),frequency_hz=tags.get('frequency'),
        source_url=url,source='OpenStreetMap · objet exact',source_timestamp=row.get('timestamp'),source_version=row.get('version'))
    asset['voltage_label']=voltage_label(asset)
    return {k:v for k,v in asset.items() if v is not None}


def voltage_label(asset):
    """Shared label for raw, imported and OSM winding voltages; no guessing."""
    from france_pv import voltage_in_volts,osm_voltage_values
    primary=asset.get("primary_voltage_v")
    secondary=asset.get("secondary_voltage_v")
    tertiary=asset.get("tertiary_voltage_v")
    tags=asset.get("osm_tags",{})
    primary=primary if primary is not None else voltage_in_volts(tags.get("voltage:primary"))
    secondary=secondary if secondary is not None else voltage_in_volts(tags.get("voltage:secondary"))
    parts=[]
    if primary is not None:
        parts.append(f"primaire {primary/1000:g} kV")
    if secondary is not None:
        parts.append(f"secondaire {secondary/1000:g} kV")
    if tertiary is not None:
        parts.append(f"tertiaire {tertiary/1000:g} kV")
    nominal=osm_voltage_values(asset.get("voltage"))
    if nominal and not (primary is not None and nominal==[primary]):
        parts.insert(0," / ".join(f"{v/1000:g}" for v in nominal)+" kV")
    if not parts:
        values=asset.get("voltage_values_v",[])
        if values:
            parts.append(" / ".join(f"{v/1000:g}" for v in dict.fromkeys(values))+" kV")
    if not parts and asset.get("voltage_raw") and any(sign in str(asset["voltage_raw"]) for sign in ("<",">")):
        parts.append("Tension source : "+str(asset["voltage_raw"]))
    return " · ".join(parts) if parts else "Tension non publiée par cette source"


def electrical_summary(frame):
    """Readable values first; full provenance remains available in exports."""
    if frame.empty:
        return frame
    known=pd.Series(False,index=frame.index)
    for column in ("Tension nominale (kV)","Tension primaire (kV)","Tension secondaire (kV)","Puissance assignée (kVA)"):
        if column in frame:
            known |= frame[column].notna()
    if "Tensions OSM (kV)" in frame:
        known |= frame["Tensions OSM (kV)"].fillna("").ne("")
    frame=frame.assign(_known=known).sort_values(["_known","Distance directe (m)"],ascending=[False,True]).drop(columns="_known")
    columns=["Identifiant","Type","Tension nominale (kV)","Tension de conception (kV)","Tension primaire (kV)",
             "Tension secondaire (kV)","Tensions OSM (kV)","Puissance assignée (kVA)",
             "Source","Distance directe (m)","Lien OSM"]
    return frame[[c for c in columns if c in frame.columns]]


def rating_kva(value, bare_unit="VA"):
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(GVA|MVA|kVA|VA)?\s*", str(value), flags=re.I)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or bare_unit).upper()
    result = number * {"VA": .001, "KVA": 1, "MVA": 1000, "GVA": 1000000}[unit]
    return result if math.isfinite(result) and result > 0 else None


def enrich_transformers(assets, area, progress=lambda _: None, maximum=3):
    """Read electrical values across the whole project area, including lines."""
    import france_pv as core
    from osm_sources import osm_map_data
    result = {a["id"]: a for a in assets}
    try:
        added = core.electrical_osm(area, progress)
        for asset in added:
            if asset["geometry"].intersects(area):
                result[asset["id"]] = {**result.get(asset["id"], {}), **asset}
        progress(f"OSM zone complète : {len(added)} ouvrages, {sum(bool(a.get('voltage_values_v')) or a.get('secondary_voltage_v') is not None for a in added)} avec tension renseignée.")
        from transmission_sources import rte_reference_sites
        try:
            for site in rte_reference_sites(list(result.values()),progress):
                result[site["id"]]=site
        except (ValueError,requests.RequestException) as exc:
            progress(f"Complément RTE indisponible : {exc}. Valeurs précédentes conservées.")
        return list(result.values())
    except (ValueError, requests.RequestException) as exc:
        progress(f"OSM zone complète indisponible ({exc}) ; extraits locaux en secours.")
    center = area.representative_point()
    targets = sorted([a for a in assets if a["kind"] in {"transformer", "substation"} and a.get("power_kva") is None],
                     key=lambda a: a["geometry"].distance(center))[:maximum]
    for i, asset in enumerate(targets):
        progress(f"Caractéristiques réelles des transformateurs : lecture OSM {i+1}/{len(targets)}…")
        try:
            data = osm_map_data(asset["geometry"].representative_point().buffer(150), progress, maximum=1)
            added, _, _ = core.parse_osm(data)
            for a in added:
                if a["geometry"].intersects(area):
                    result[a["id"]] = a
        except (ValueError, requests.RequestException) as exc:
            # Do not turn unavailable enrichment into a failed site study.
            progress(f"Complément de plaque indisponible : {type(exc).__name__}. Valeurs déjà obtenues conservées.")
            break
    return list(result.values())

"""Bounded OSM XML extracts used by the French network fallback."""
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import numpy as np
import requests
from shapely.geometry import box
import france_pv as core

def xml_response(url, params=None, progress=lambda _: None):
    core.CACHE.mkdir(parents=True, exist_ok=True)
    path = core.CACHE / ("xml_" + hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest() + ".xml")
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        try:
            return ET.fromstring(path.read_bytes())
        except ET.ParseError:
            pass
    cooldown=core.CACHE/('xml_cooldown_'+hashlib.sha256(urlparse(url).netloc.encode()).hexdigest()+'.json')
    if cooldown.exists():
        try:
            remaining=float(json.loads(cooldown.read_text())['until'])-time.time()
        except (OSError,ValueError,KeyError):remaining=0
        if remaining>0:
            raise ValueError(f'{urlparse(url).netloc} temporairement indisponible ; nouvel essai dans {int(remaining)+1} s. Les réponses déjà en cache restent utilisables.')
    for attempt in range(2):
        response = None
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; SolarAtlas/1.0)", "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5"}
            response = requests.get(url, params=params, timeout=(8, 40), headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                delay = core.retry_delay(response.headers.get("Retry-After"), 5)
                if attempt or delay > 30:
                    cooldown.write_text(json.dumps({'until':time.time()+delay}))
                    raise ValueError(f"{urlparse(url).netloc} : HTTP {response.status_code}, réessayer dans {int(delay)} s.")
                core.wait_until(time.time() + delay, progress, urlparse(url).netloc, "serveur occupé")
                continue
            response.raise_for_status()
            root = ET.fromstring(response.content)
            errors = [e.text for e in root.iter() if e.tag.split("}")[-1] in {"ExceptionText", "ServiceException", "error"}]
            if errors:
                raise ValueError("Service cartographique : " + "; ".join(str(e) for e in errors))
            path.write_bytes(response.content)
            return root
        except (requests.RequestException, ET.ParseError) as exc:
            if attempt:
                cooldown.write_text(json.dumps({'until':time.time()+60}))
                raise ValueError(f"{urlparse(url).netloc} : {type(exc).__name__}, après deux essais.") from exc
            core.wait_until(time.time() + 3, progress, urlparse(url).netloc, "connexion interrompue")
        finally:
            if response is not None:
                response.close()


def osm_map_data(area, progress=lambda _: None, maximum=20):
    """Alternative to Overpass, for small local map extracts, never bulk harvest."""
    x0,y0,x1,y1 = area.bounds
    tiles = [box(x,y,x+2000,y+2000).intersection(area) for x in np.arange(x0,x1,2000) for y in np.arange(y0,y1,2000)]
    tiles = [t for t in tiles if not t.is_empty and t.area > 0]
    if len(tiles) > maximum:
        raise ValueError(f"Lecture OSM locale limitée à {maximum} tuiles ; réduire le rayon réseau.")
    elements = {}
    for i, tile in enumerate(tiles):
        progress(f"API de lecture OSM : tuile {i+1}/{len(tiles)}…")
        bbox = ",".join(str(v) for v in core.geographic(tile).bounds)
        root = xml_response("https://api.openstreetmap.org/api/0.6/map", {"bbox": bbox}, progress)
        nodes = {e.get("id"): (float(e.get("lon")), float(e.get("lat"))) for e in root.findall("node")}
        for element in list(root.findall("node")) + list(root.findall("way")):
            tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
            if not tags:
                continue
            record = dict(type=element.tag, id=element.get("id"), tags=tags)
            if element.tag == "node":
                record.update(lon=float(element.get("lon")), lat=float(element.get("lat")))
            else:
                refs = [n.get("ref") for n in element.findall("nd")]
                if any(ref not in nodes for ref in refs):
                    raise ValueError("OSM : géométrie de voie incomplète, extrait non accepté.")
                record["geometry"] = [dict(lon=nodes[ref][0], lat=nodes[ref][1]) for ref in refs]
            elements[(record["type"], record["id"])] = record
    return {"elements": list(elements.values())}



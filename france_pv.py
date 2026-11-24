"""French PV screening: metric geometry, real public data, explicit assumptions."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import threading
import tempfile
import sqlite3
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests
from backend.domain.geometry import METRIC_CRS, TO_METRIC, TO_GEO, metric, geographic, point_identity
from backend.domain.costs import capex, connection, voltage_in_volts
from shapely.geometry import Point, LineString, shape, mapping, box
from shapely.ops import transform, unary_union, nearest_points

CACHE = Path(__file__).parent / "data" / "france_cache"
OVERPASS_ENDPOINTS = ("https://overpass.private.coffee/api/interpreter", "https://overpass-api.de/api/interpreter")
OVERPASS_LOCK = threading.Lock()
METEO_LOCK = threading.Lock()
# Conservative local budgets, expressed in estimated weighted API calls.
METEO_LIMITS = ((60, 450), (3600, 4000), (86400, 9000))
METEO_REQUEST_WEIGHT = 40
METEO_LIMIT_STATE_VERSION = 2
METEO_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
METEO_MODEL = "meteofrance_arome_france"
FIRST_SOLAR_YEAR = 2022  # GFS archive starts March 2021: first full year.


def solar_model(preferred, start_year):
    # Use one model for the whole requested period, including its cache keys.
    return "gfs_global" if start_year < 2025 else preferred
EQUIPMENT_LABELS = {"transformer": "Transformateur", "substation": "Poste électrique",
                    "line": "Ligne aérienne", "minor_line": "Ligne aérienne", "cable": "Liaison souterraine"}


def retry_delay(value, default):
    """Accept Retry-After as seconds or an HTTP date, without shortening it."""
    try:
        return max(default, float(value))
    except (TypeError, ValueError):
        try:
            return max(default, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default


def wait_until(deadline, progress, service="OSM", reason="pause avant nouvel essai"):
    while (remaining := deadline - time.time()) > 0:
        progress(f"{service} : {reason} — {math.ceil(remaining)} s restantes.")
        time.sleep(min(5, remaining))


def overpass(query, progress=lambda message: None):
    """Serialize calls, persist server cooldowns and cache complete results for 24 h."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / ("osm_" + hashlib.sha256(query.encode()).hexdigest() + ".json")
    with OVERPASS_LOCK:
        if cached.exists() and time.time() - cached.stat().st_mtime < 86400:
            progress("OSM : données réutilisées depuis le cache (moins de 24 h).")
            return json.loads(cached.read_text(encoding="utf-8"))
        failures = []
        # Rotate servers between rounds; never retry a server before its deadline.
        for attempt in range(2):
            for endpoint in OVERPASS_ENDPOINTS:
                state = CACHE / ("cooldown_" + hashlib.sha256(endpoint.encode()).hexdigest() + ".json")
                response = None
                try:
                    deadline = float(json.loads(state.read_text())["until"]) if state.exists() else 0
                    if deadline - time.time() > 45:
                        failures.append(f"{endpoint} : quota, réessayer dans {math.ceil(deadline - time.time())} s")
                        continue
                    # Try the other host first instead of waiting on a busy one.
                    if attempt == 0 and deadline > time.time():
                        continue
                    wait_until(deadline, progress)
                    progress(f"OSM : {endpoint.split('/')[2]}, tentative {attempt + 1}/2…")
                    response = requests.get(endpoint, params={"data": query}, timeout=(5, 40))
                    if response.status_code == 429 or response.status_code >= 500:
                        delay = retry_delay(response.headers.get("Retry-After"), 30 * 2 ** attempt)
                        state.write_text(json.dumps({"until": time.time() + delay}))
                        failures.append(f"{endpoint} : HTTP {response.status_code}, pause {math.ceil(delay)} s")
                        progress(f"OSM : serveur occupé (HTTP {response.status_code}), pause de {math.ceil(delay)} s avant réutilisation.")
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict) or "elements" not in data or data.get("remark"):
                        raise ValueError("Réponse OSM incomplète")
                    state.write_text(json.dumps({"until": time.time() + 1}))
                    cached.write_text(json.dumps(data), encoding="utf-8")
                    return data
                except (requests.RequestException, ValueError) as exc:
                    state.write_text(json.dumps({"until": time.time() + 15 * 2 ** attempt}))
                    failures.append(f"{endpoint} : {type(exc).__name__}")
                finally:
                    if response is not None:
                        response.close()
        raise ValueError("Contexte OSM indisponible après deux tours bornés. " + " ; ".join(failures[-2:]))


def streamed_meteo(params, progress):
    """Receive chunks into a temporary file, close even on a mid-stream failure."""
    response = requests.get(METEO_URL, params=params, timeout=(10, 45), stream=True)
    try:
        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = {}
            return response.status_code, response.headers, data
        progress("Open-Meteo : réponse reçue, lecture du flux…")
        received, started, reported = 0, time.monotonic(), 0.
        with tempfile.TemporaryFile(dir=CACHE) as output:
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                received += len(chunk)
                output.write(chunk)
                if time.monotonic() - started > 180:
                    raise requests.ReadTimeout("durée totale du flux supérieure à 180 s")
                if time.monotonic() - reported >= .5:
                    progress(f"Open-Meteo : réception du flux, {received / 1024:.0f} Kio reçus…")
                    reported = time.monotonic()
            output.seek(0)
            data = json.load(output)
        progress(f"Open-Meteo : {received / 1024:.0f} Kio téléchargés ; contrôle des heures et de l'irradiance.")
        return response.status_code, response.headers, data
    finally:
        response.close()


def meteo_weight(params):
    days = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
    locations = len(str(params["latitude"]).split(","))
    variables = len(str(params.get("hourly", "")).split(","))
    models = len(str(params.get("models", METEO_MODEL)).split(","))
    # Open-Meteo ForecastApiResult.calculateQueryWeight(): fractional accounting,
    # with the minimum applied once per location, not to each multiplier.
    return locations * max(1., variables * models / 10 * max(1., days / 14))


def meteo_payload_weight(params):
    """Transport-size heuristic, kept separate from the provider's call accounting.

    Preserve the former chunk boundaries so completed downloads remain reusable.
    """
    days=(date.fromisoformat(params['end_date'])-date.fromisoformat(params['start_date'])).days+1
    locations=len(str(params['latitude']).split(','))
    variables=len(str(params.get('hourly','')).split(','))
    models=len(str(params.get('models',METEO_MODEL)).split(','))
    return math.ceil(locations*models*max(1,variables/10)*max(1,days/14*1.5))


def meteo_limit_state(path,now,progress):
    """Migrate inaccurate old estimates, preserving confirmed server cooldowns."""
    try:state=json.loads(path.read_text()) if path.exists() else {}
    except (ValueError,OSError):state={}
    if not isinstance(state,dict):state={}
    until=state.get('until',0)
    state['until']=until if isinstance(until,(int,float)) and math.isfinite(until) else 0
    events=[]
    raw_events=state.get('events',[])
    for event in raw_events if isinstance(raw_events,list) else []:
        if (isinstance(event,list) and len(event)>=2 and isinstance(event[0],(int,float))
                and isinstance(event[1],(int,float)) and math.isfinite(event[0]) and math.isfinite(event[1])
                and event[1]>0 and now-86400<event[0]<=now):
            events.append([event[0],event[1],event[2] if len(event)>2 else 'legacy'])
    if state.get('version')!=METEO_LIMIT_STATE_VERSION:
        if path.exists():progress('Open-Meteo : correction du compteur local historique ; caches et pauses serveur conservés.')
        if str(state.get('reason','')).startswith('régulation locale'):
            state['until']=now
            state['reason']='espacement local entre téléchargements'
        # Old weights have no request parameters and cannot be reconstructed.
        # Keep them for audit and conservative minute pacing only, never as an
        # authoritative hourly/daily consumption total.
        events=[e[:2]+['legacy'] for e in events]
    state.update(version=METEO_LIMIT_STATE_VERSION,events=events)
    return state


def validate_irradiance(location, params):
    hourly = location.get("hourly", {})
    times, values = hourly.get("time", []), hourly.get("global_tilted_irradiance", [])
    expected = pd.date_range(params["start_date"], date.fromisoformat(params["end_date"]) + timedelta(days=1), freq="h", inclusive="left")
    if (len(times) != len(expected) or len(values) != len(expected)
            or not pd.DatetimeIndex(times).equals(expected)
            or any(v is None or not math.isfinite(v) or v < 0 for v in values)):
        raise ValueError("Open-Meteo : irradiance incomplète ou invalide pour ce modèle et cette période. Aucun segment invalide n'est conservé.")


def meteo_request(params, progress):
    """Persist a weighted rolling budget and server cooldown across reruns."""
    state_path = CACHE / "open_meteo_limits.json"
    cost = meteo_weight(params)
    with METEO_LOCK:
        for attempt in range(4):
            now = time.time()
            state = meteo_limit_state(state_path,now,progress)
            events = state['events']
            deadline = max(now, state.get("until", 0))
            reason = state.get("reason", "espacement local entre téléchargements")
            for window, budget in METEO_LIMITS:
                active = sorted(e for e in events if e[0] > now - window and (window==60 or e[2]!='legacy'))
                total = sum(e[1] for e in active) + cost
                for timestamp, weight, *_ in active:
                    if total <= budget:
                        break
                    total -= weight
                    candidate = timestamp + window + 1
                    if candidate > deadline:
                        deadline = candidate
                        reason = f"régulation locale ({window}s), pas une erreur serveur"
            if deadline - now > 120:
                raise ValueError(f"Open-Meteo : {reason}, attente de {math.ceil((deadline - now) / 60)} min. Arrêt sans nouvel appel ; téléchargements terminés conservés.")
            wait_until(deadline, progress, "Open-Meteo", reason=reason)
            # Reserve before sending; failed requests may count towards the quota.
            events.append([time.time(), cost, 'v2'])
            state = {"version":METEO_LIMIT_STATE_VERSION,"events": events, "until": time.time() + 1, "reason": "espacement local entre téléchargements"}
            state_path.write_text(json.dumps(state))
            progress(f"Open-Meteo : téléchargement, tentative {attempt + 1}/4 (charge estimée {cost:g}).")
            try:
                status_code, headers, result = streamed_meteo(params, progress)
            except requests.RequestException as exc:
                delay = 15 * 2 ** attempt
                state["until"] = time.time() + delay
                state["reason"] = f"reprise après {type(exc).__name__}"
                state_path.write_text(json.dumps(state))
                progress(f"Open-Meteo : téléchargement interrompu ({type(exc).__name__}), pause de {delay} s avant reprise du segment.")
                if attempt == 3:
                    raise ValueError(f"Open-Meteo : téléchargement interrompu après 4 tentatives ({type(exc).__name__}). Relancez après {delay} s ; les segments terminés sont conservés.") from exc
                continue
            except (ValueError, UnicodeError) as exc:
                state["until"] = time.time() + 15 * 2 ** attempt
                state["reason"] = "reprise après réponse JSON illisible"
                state_path.write_text(json.dumps(state))
                if attempt == 3:
                    raise ValueError("Open-Meteo : réponses illisibles après 4 tentatives ; segments terminés conservés.") from exc
                continue
            if status_code == 429 or status_code >= 500:
                reason = str(result.get("reason", "")).lower() if isinstance(result, dict) else ""
                default = 65 * 2 ** attempt
                if "hour" in reason:
                    default = max(default, 3605)
                if "day" in reason or "daily" in reason:
                    default = max(default, 86405)
                retry_after=headers.get('Retry-After')
                if retry_after is None:delay=default
                else:
                    try:delay=max(1,float(retry_after))
                    except (TypeError,ValueError):
                        try:delay=max(1,(parsedate_to_datetime(retry_after)-datetime.now(timezone.utc)).total_seconds())
                        except (TypeError,ValueError,OverflowError):delay=default
                state["until"] = time.time() + delay
                state["reason"] = f"quota serveur HTTP {status_code}" if status_code == 429 else f"serveur indisponible HTTP {status_code}"
                state_path.write_text(json.dumps(state))
                progress(f"Open-Meteo : HTTP {status_code}, pause serveur de {math.ceil(delay)} s.")
                if attempt >= 1 or delay > 120:
                    raise ValueError(f"Open-Meteo : quota ou service indisponible, réessayez dans {math.ceil(delay)} s. Les téléchargements terminés sont conservés.")
                continue
            if 400 <= status_code < 500:
                reason = result.get("reason", "requête refusée") if isinstance(result, dict) else "requête refusée"
                raise ValueError(f"Open-Meteo : HTTP {status_code} — {reason}. Vérifiez modèle et période ; aucun nouvel essai automatique pour cette requête.")
            if isinstance(result, dict) and result.get("error"):
                raise ValueError(f"Open-Meteo : {result.get('reason', 'requête refusée')}")
            locations = result if isinstance(result, list) else [result]
            if len(locations) != len(str(params["latitude"]).split(",")):
                raise ValueError("Open-Meteo : nombre de lieux incohérent, réponse non conservée.")
            for location in locations:
                validate_irradiance(location, params)
            return result


def meteo_data(params, progress):
    """Split large requests; each complete subrequest has its own reusable cache."""
    if meteo_payload_weight(params) <= METEO_REQUEST_WEIGHT:
        return meteo_request(params, progress)
    latitudes, longitudes = str(params["latitude"]).split(","), str(params["longitude"]).split(",")
    if len(latitudes) > 1:
        middle = len(latitudes) // 2
        combined = []
        for indices in (slice(None, middle), slice(middle, None)):
            part = {**params, "latitude": ",".join(latitudes[indices]), "longitude": ",".join(longitudes[indices])}
            data = api(METEO_URL, part, progress=progress)
            combined.extend(data if isinstance(data, list) else [data])
        return combined
    start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
    middle = start + (end - start) // 2
    first = api(METEO_URL, {**params, "end_date": middle.isoformat()}, progress=progress)
    second = api(METEO_URL, {**params, "start_date": (middle + timedelta(days=1)).isoformat()}, progress=progress)
    first = first[0] if isinstance(first, list) else first
    second = second[0] if isinstance(second, list) else second
    return {**first, "hourly": {key: values + second["hourly"][key] for key, values in first["hourly"].items()}}


def api(url, params=None, payload=None, progress=lambda message: None):
    key = hashlib.sha256(json.dumps([url, params, payload], sort_keys=True).encode()).hexdigest()
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{key}.json"
    daily_source = urlparse(url).netloc in {"api.openstreetmap.org","opendata.enedis.fr", "data.geopf.fr", "www.arcgis.com", "services9.arcgis.com", "sigpac-hubcloud.es","odre.opendatasoft.com","georedservices.ree.es","bio.discomap.eea.europa.eu","apicarto.ign.fr"}
    if path.exists() and (not daily_source or time.time() - path.stat().st_mtime < 86400):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            progress("Cache illisible : récupération d'une nouvelle réponse complète.")
    if url == METEO_URL:
        result = meteo_data(params, progress)
    else:
        result = public_json(url, params=params, payload=payload, progress=progress)
    if isinstance(result, dict) and result.get("error"):
        raise ValueError(result.get("reason", str(result["error"])))
    # Do not cache partial Overpass results.
    if isinstance(result, dict) and result.get("remark"):
        raise ValueError(result["remark"])
    path.write_text(json.dumps(result), encoding="utf-8")
    return result


def public_json(url, params=None, payload=None, progress=lambda message: None):
    """Bound retries for IGN, administrative and Enedis requests too."""
    host = urlparse(url).netloc
    for attempt in range(2):
        response = None
        try:
            response = (requests.get(url, params=params, timeout=(8, 45)) if payload is None
                        else requests.post(url, json=payload, timeout=(8, 45)))
            if response.status_code == 429 or response.status_code >= 500:
                delay = retry_delay(response.headers.get("Retry-After"), 5)
                if attempt or delay > 30:
                    raise ValueError(f"{host} : HTTP {response.status_code}, réessayez dans {math.ceil(delay)} s.")
                wait_until(time.time() + delay, progress, host, "serveur occupé")
                continue
            if 400 <= response.status_code < 500:
                raise ValueError(f"{host} : HTTP {response.status_code}, requête refusée ; aucun nouvel essai automatique.")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            if attempt:
                raise ValueError(f"{host} : service injoignable ({type(exc).__name__}), après 2 essais.") from exc
            wait_until(time.time() + 3, progress, host, "connexion interrompue")
        finally:
            if response is not None:
                response.close()


def gps_area(lat, lon, radius):
    if not .1<=radius<=1000:
        raise ValueError('Le rayon doit être compris entre 0,1 et 1 000 km.')
    if not (41 <= lat <= 51.5 and -5.5 <= lon <= 10):
        raise ValueError("Le point doit se situer en France métropolitaine (Corse comprise).")
    # Administrative lookup rejects offshore/foreign center points.
    center = api("https://geo.api.gouv.fr/communes", {"lat": lat, "lon": lon})
    if not center:
        raise ValueError("Ce point n'appartient pas à une commune française.")
    regions = api("https://etalab-datasets.geo.data.gouv.fr/contours-administratifs/2026/geojson/regions-100m.geojson")
    mainland_box = box(-5.5, 41, 10, 51.5)
    france = unary_union([metric(shape(f["geometry"]).buffer(0).intersection(mainland_box)).buffer(0)
                          for f in regions["features"]])
    return metric(Point(lon, lat)).buffer(radius * 1000).intersection(france)


def grid(area, spacing):
    step = spacing * 1000
    x0, y0, x1, y1 = area.bounds
    points = [Point(x, y) for x in np.arange(x0 + step / 2, x1, step)
              for y in np.arange(y0 + step / 2, y1, step) if area.covers(Point(x, y))]
    return points or [area.representative_point()]


def solar(points, start_year, end_year, tilt, pr, progress=lambda message: None, *, model=METEO_MODEL):
    model = solar_model(model, start_year)
    if start_year < FIRST_SOLAR_YEAR or end_year >= date.today().year or start_year > end_year:
        raise ValueError(f"Historical Forecast ({model}) : choisir des années complètes entre {FIRST_SOLAR_YEAR} et {date.today().year - 1}.")
    rows = []
    for offset in range(0, len(points), 10):
        batch = points[offset:offset + 10]
        coords = [TO_GEO(p.x, p.y) for p in batch]
        progress(f"Open-Meteo : {offset + 1}–{offset + len(batch)} / {len(points)} points")
        data = api(METEO_URL, {
            "latitude": ",".join(str(lat) for lon, lat in coords),
            "longitude": ",".join(str(lon) for lon, lat in coords),
            "start_date": f"{start_year}-01-01", "end_date": f"{end_year}-12-31",
            "hourly": "global_tilted_irradiance", "tilt": tilt, "azimuth": 0,
            "models": model, "timezone": "UTC"}, progress=progress)
        locations = data if isinstance(data, list) else [data]
        if len(locations) != len(batch):
            raise ValueError("Réponse météo incomplète.")
        for p, location in zip(batch, locations):
            hourly = location.get("hourly", {})
            values, times = hourly.get("global_tilted_irradiance", []), hourly.get("time", [])
            expected = (date(end_year + 1, 1, 1) - date(start_year, 1, 1)).days * 24
            timeline = pd.date_range(f"{start_year}-01-01", f"{end_year + 1}-01-01", freq="h", inclusive="left")
            if len(values) != expected or len(times) != expected or not pd.DatetimeIndex(times).equals(timeline) or any(v is None or not math.isfinite(v) or v < 0 for v in values):
                raise ValueError("Série météo incomplète ou invalide : estimation interrompue.")
            annual = pd.Series(values, index=pd.to_datetime(times)).groupby(lambda t: t.year).sum() / 1000 * pr
            monthly=pd.Series(values,index=pd.to_datetime(times)).groupby(lambda t:t.month).sum()/1000/(end_year-start_year+1)
            lon, lat = TO_GEO(p.x, p.y)
            rows.append({"x": p.x, "y": p.y, "longitude": lon, "latitude": lat,
                         "yield": float(annual.mean()), "annual_min": float(annual.min()),
                         "irradiance_kwh_m2": float(annual.mean()) / pr,
                         "irradiance_mean_w_m2": float(sum(values) / len(values)),
                         "monthly_irradiance_kwh_m2":[float(monthly.get(m,0)) for m in range(1,13)]})
    return pd.DataFrame(rows)


class SolarEvaluator:
    """One finite point budget shared by search and connection comparison."""
    def __init__(self, start, end, tilt, pr, progress, maximum=40, model=METEO_MODEL,persistent=False,reuse_distance_m=0):
        self.settings = start, end, tilt, pr
        self.progress, self.maximum = progress, maximum
        self.records = {}
        self.model = solar_model(model, start)
        self.persistent=persistent
        self.cache_hits=0
        from solar_store import SolarStore
        self.store=SolarStore(CACHE/'solar.sqlite3')
        self.reuse_distance_m=min(max(reuse_distance_m,0),1000)
        self.db_hits=0
        self.nearby_hits=0
        self.api_points=0
        self.storage_warning=False

    def cache_path(self,point):
        key=json.dumps(["solar-point-v1",self.model,self.settings[:3],point_identity(point)])
        return CACHE/"solar_points"/(hashlib.sha256(key.encode()).hexdigest()+".json")

    def load_point(self,point):
        from solar_store import valid_record
        row=None
        try:
            row=self.store.get(self.model,*self.settings[:3],point.x,point.y)
        except (sqlite3.Error,OSError):
            self.warn_storage()
        try:
            if row is None:
                legacy=json.loads(self.cache_path(point).read_text(encoding="utf-8"))
                if valid_record(legacy):
                    row=legacy
                    try:self.store.put(self.model,*self.settings[:3],point.x,point.y,legacy,origin='Cache JSON antérieur')
                    except (sqlite3.Error,OSError):self.warn_storage()
        except (OSError,ValueError,TypeError):pass
        if row is None and self.reuse_distance_m:
            try:row=self.store.get(self.model,*self.settings[:3],point.x,point.y,self.reuse_distance_m)
            except (sqlite3.Error,OSError):self.warn_storage()
        if row is None:return None
        try:
            pr=self.settings[3]
            for key in ("base_yield","base_min"):
                if not isinstance(row.get(key),(int,float)) or not math.isfinite(row[key]) or row[key]<0:
                    return None
            for key in ("irradiance_kwh_m2","irradiance_mean_w_m2"):
                if key in row and (not isinstance(row[key],(int,float)) or not math.isfinite(row[key]) or row[key]<0):
                    return None
            if "monthly_irradiance_kwh_m2" in row:
                months=row["monthly_irradiance_kwh_m2"]
                if not isinstance(months,list) or len(months)!=12 or any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in months):
                    return None
            row["yield"]=row.pop("base_yield")*pr
            row["annual_min"]=row.pop("base_min")*pr
            row["x"],row["y"]=point.x,point.y
            if row.get('weather_storage')=='SQLite':self.db_hits+=1
            if row.get('weather_reuse_distance_m',0)>.02:self.nearby_hits+=1
            return row
        except (OSError,ValueError,TypeError):
            return None

    def save_point(self,point,row):
        path=self.cache_path(point)
        record=dict(row)
        record["base_yield"]=float(record.pop("yield"))/self.settings[3]
        record["base_min"]=float(record.pop("annual_min",record["base_yield"]*self.settings[3]))/self.settings[3]
        try:
            self.store.put(self.model,*self.settings[:3],point.x,point.y,record)
            return
        except (sqlite3.Error,OSError):self.warn_storage()
        temporary=None
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as output:
                temporary=Path(output.name)
                json.dump(record,output)
            temporary.replace(path)
        except OSError:
            self.progress("Cache météo local non inscriptible ; résultat calculé conservé.")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def warn_storage(self):
        if not self.storage_warning:
            self.progress('Base solaire locale indisponible ; cache fichiers et calcul conservés.')
            self.storage_warning=True

    @property
    def remaining(self):
        return self.maximum - len(self.records)

    def __call__(self, points):
        unique = {point_identity(p): p for p in points}
        missing = [p for key, p in unique.items() if key not in self.records]
        if len(missing) > self.remaining:
            raise ValueError("Budget de points météo atteint ; aucun appel supplémentaire envoyé.")
        if self.persistent:
            for point in missing:
                row=self.load_point(point)
                if row is not None:
                    self.records[point_identity(point)]=row
                    self.cache_hits+=1
            missing=[p for p in missing if point_identity(p) not in self.records]
            self.progress(f"Cache météo : {self.cache_hits} points réutilisés, {len(missing)} nouveaux points à récupérer.")
        if missing:
            self.progress(f"Budget météo : {len(self.records) + len(missing)}/{self.maximum} points, irradiance uniquement.")
            frame = (solar(missing, *self.settings, self.progress) if self.model == METEO_MODEL
                     else solar(missing, *self.settings, self.progress, model=self.model))
            self.api_points+=len(missing)
            self.api_points+=len(missing)
            for p, row in zip(missing, frame.to_dict("records")):
                self.records[point_identity(p)] = row
                if self.persistent:
                    self.save_point(p,row)
        return pd.DataFrame([self.records[key] for key in unique])


def spread_points(points, limit):
    """Deterministic farthest-point subset avoids clustering the initial samples."""
    if len(points) <= limit:
        return points
    selected = [points[len(points) // 2]]
    remaining = [p for p in points if p != selected[0]]
    while len(selected) < limit and remaining:
        best = max(remaining, key=lambda p: min(p.distance(s) for s in selected))
        selected.append(best)
        remaining.remove(best)
    return selected


def search_solar(area, start_year, end_year, tilt, pr, progress, *, evaluator=None, point_budget=32):
    evaluator = evaluator or SolarEvaluator(start_year, end_year, tilt, pr, progress, point_budget)
    point_budget = min(point_budget, evaluator.remaining)
    if point_budget < 12:
        raise ValueError("Au moins 12 points sont nécessaires à la recherche progressive.")
    x0, y0, x1, y1 = area.bounds
    spacing = max(2., math.ceil(max(x1 - x0, y1 - y0) / 4000))
    steps = [spacing]
    while steps[-1] > 1:
        steps.append(max(1., steps[-1] / 2))
    initial_limit = min(12, point_budget - 2 * (len(steps) - 1))
    initial = spread_points(grid(area, spacing), max(1, initial_limit))
    frames, seen, used = [], set(), 0
    for index, step in enumerate(steps):
        if index == 0:
            points = initial
        else:
            ranked = pd.concat(frames, ignore_index=True).sort_values("yield", ascending=False)
            seeds = []
            for row in ranked.itertuples():
                p = Point(row.x, row.y)
                if all(p.distance(s) >= step * 1000 for s in seeds):
                    seeds.append(p)
                if len(seeds) == 2:
                    break
            # Interleave neighbours from both basins; never scan their whole disks.
            points = []
            offsets = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
            for dx, dy in offsets:
                for seed in seeds:
                    p = Point(seed.x + dx * step * 1000, seed.y + dy * step * 1000)
                    if area.covers(p) and point_identity(p) not in seen:
                        points.append(p)
            limit = max(1, (point_budget - used) // (len(steps) - index))
            points = list({point_identity(p): p for p in points}.values())[:min(8, limit)]
        if not points:
            continue
        progress(f"Grille {step:g} km : {len(points)} nouveaux points, plafond recherche {point_budget}.")
        frame = evaluator(points)
        frame["pas_km"] = step
        frames.append(frame)
        seen.update(point_identity(p) for p in points)
        used += len(points)
    result = pd.concat(frames, ignore_index=True)
    return result, result.sort_values("yield", ascending=False).iloc[0].to_dict()


def enedis_assets(area, progress=lambda message: None):
    """Independent official source: Data Fair API, with bounded pagination."""
    bbox = ",".join(str(v) for v in geographic(area).bounds)
    assets, errors = [], []
    datasets = [("poste-electrique", "transformer"),
                ("dld8womlmzxpcox425-7yhma", "cable"),
                ("7p9-paqaf6cckipnjiubcncw", "line")]
    for dataset, kind in datasets:
        progress(f"Enedis : lecture {kind} dans la zone…")
        url = f"https://opendata.enedis.fr/data-fair/api/v1/datasets/{dataset}/lines"
        # Data Fair omits spatial fields from its default response.
        params = {"bbox": bbox, "size": 1000, "select": "_id,_geoshape,geometry,nom_commune"}
        collected = []
        try:
            for page in range(20):
                data = api(url, params, progress=progress)
                for row in data.get("results", []):
                    geometry = row.get("_geoshape") or row.get("geometry") or row.get("_geometry")
                    if isinstance(geometry, str):
                        geometry = json.loads(geometry)
                    if not geometry or not row.get('_id'):
                        continue
                    geom = metric(shape(geometry))
                    if not geom.is_empty and area.intersects(geom):
                        collected.append({"id": f"Enedis/{dataset}/{row.get('_id', len(collected))}",
                                          "kind": kind, "voltage": row.get("tension"),
                                          "voltage_class": "HTA/BT" if kind == "transformer" else "HTA",
                                          "power_kva": row.get("puissance_kva"), "current_a": row.get("intensite_a"),
                                          "commune": row.get("nom_commune"), "source": "Enedis", "geometry": geom,
                                          "source_url": f"https://opendata.enedis.fr/datasets/{dataset}",
                                          "data_scope": "Localisation officielle ; tension et puissance non publiées dans ce jeu"})
                following = data.get("next")
                if not following:
                    assets.extend(collected)
                    break
                parsed = urlparse(following)
                if parsed.scheme != "https" or parsed.netloc != "opendata.enedis.fr":
                    raise ValueError("Pagination Enedis invalide")
                url, params = following, None
            else:
                raise ValueError("Pagination Enedis au-delà de 20 pages, réduire la zone")
        except (ValueError, requests.RequestException) as exc:
            errors.append(str(exc))
    if not assets and errors:
        raise ValueError("Enedis indisponible : " + "; ".join(errors))
    if errors:
        progress("Enedis : couverture partielle ; certains jeux n'ont pas répondu.")
    return assets


def network(area, progress=lambda message: None, include_context=True):
    """Enedis first. OSM is only a fallback for electrical assets."""
    try:
        assets = enedis_assets(area, progress)
    except ValueError as exc:
        progress(str(exc))
        assets = []
    try:
        # IGN contains nominal voltages for transport lines, unlike the
        # Enedis location layers. Keep source IDs distinct; never transfer
        # a nearby object's voltage onto a different Enedis object.
        assets += ign_electrical_assets(area, progress)
    except (ValueError, requests.RequestException) as exc:
        progress(f"Complément électrique IGN indisponible : {exc}. Ouvrages déjà récupérés conservés.")
    if not any(a.get("planning_eligible") is not False for a in assets):
        try:
            assets += electrical_osm(area, progress)
        except (ValueError, requests.RequestException):
            if not assets:
                raise
            progress("Complément OSM indisponible ; seuls les ouvrages recensés sont affichés.")
    if include_context:
        exclusions, farmland = land_context(area, progress)
    else:
        exclusions, farmland = Point().buffer(0), Point().buffer(0)
    return assets, exclusions, farmland


def ign_electrical_assets(area, progress=lambda message: None):
    assets = []
    for layer, kind in (("ligne_electrique", "line"), ("poste_de_transformation", "substation")):
        progress(f"IGN : caractéristiques électriques {layer}…")
        for feature in wfs_features("BDTOPO_V3:" + layer, area, progress):
            props = feature.get("properties", {})
            if not feature.get("geometry") or props.get("etat_de_l_objet") in {"Détruit", "Hors service"}:
                continue
            geom = metric(shape(feature["geometry"]))
            if geom.is_empty or not area.intersects(geom):
                continue
            raw_voltage = props.get("voltage")
            volts = voltage_in_volts(raw_voltage)
            voltage_class = "HTB" if volts and volts > 50000 else "HTA" if volts and volts > 1000 else "non publiée"
            assets.append({"id": "IGN/" + str(props.get("cleabs") or feature.get("id")),
                           "kind": kind, "source": "IGN BD TOPO", "geometry": geom,
                           "voltage": str(int(volts)) if volts is not None else "inconnue",
                           "voltage_raw": raw_voltage, "voltage_class": voltage_class,
                           "power_kva": None, "current_a": None,
                           "source_date": props.get("date_modification") or props.get("date_creation"),
                           "planning_eligible": voltage_class == "HTA"})
    return assets


def electrical_osm(area, progress=lambda message: None):
    west, south, east, north = geographic(area).bounds
    bbox = f"{south},{west},{north},{east}"
    query = f'''[out:json][timeout:30];(
      nwr["power"~"^(line|minor_line|cable)$"]["voltage"]({bbox});
      nwr["power"="transformer"]["voltage:primary"]({bbox});
      nwr["power"="transformer"]["voltage:secondary"]({bbox});
      nwr["power"~"^(substation|transformer)$"]({bbox});
      nwr["power"="pole"]["transformer"]({bbox});
    );out center geom;'''
    try:
        data = overpass(query, progress)
    except (ValueError, requests.RequestException):
        from osm_sources import osm_map_data
        progress("Overpass indisponible : lecture directe de petits extraits OSM.")
        data = osm_map_data(area, progress)
    assets, _, _ = parse_osm(data)
    return [a for a in assets if a["kind"] in {"transformer","substation","line","minor_line","cable"}]


def wfs_features(layer, area, progress):
    crs = "urn:ogc:def:crs:OGC:1.3:CRS84"
    bbox = ",".join(str(v) for v in geographic(area).bounds) + "," + crs
    features = []
    for page in range(20):
        data = api("https://data.geopf.fr/wfs", {"service": "WFS", "version": "2.0.0",
            "request": "GetFeature", "typeNames": layer, "outputFormat": "application/json",
            "srsName": crs, "bbox": bbox, "count": 1000, "startIndex": page * 1000}, progress=progress)
        if data.get("type") != "FeatureCollection":
            raise ValueError(f"IGN : réponse invalide pour {layer}")
        rows = data.get("features", [])
        features.extend(rows)
        matched = data.get("numberMatched", data.get("totalFeatures"))
        if len(rows) < 1000 or (str(matched).isdigit() and len(features) >= int(matched)):
            if str(matched).isdigit() and len(features) < int(matched):
                raise ValueError(f"IGN : réponse tronquée pour {layer}")
            return features
    raise ValueError(f"IGN : {layer}, plus de 20 pages ; réduire la zone")


def physical_land_context(area, progress=lambda message: None):
    """Physical obstacles from IGN, independent of agricultural eligibility."""
    exclusions = []
    for layer, margin in (("batiment", 5), ("troncon_de_route", 10),
                          ("surface_hydrographique", 5), ("zone_de_vegetation", 0)):
        progress(f"IGN BD TOPO : {layer}…")
        for feature in wfs_features("BDTOPO_V3:" + layer, area, progress):
            if not feature.get("geometry"):
                raise ValueError(f"IGN : géométrie manquante pour {layer}")
            geom = metric(shape(feature["geometry"])).buffer(margin)
            if not geom.is_empty:
                exclusions.append(geom)
    return unary_union(exclusions)


def official_land_context(area, progress=lambda message: None):
    """All required context from IGN BD TOPO and official RPG, without Overpass."""
    exclusions=physical_land_context(area,progress)
    progress("IGN : surfaces agricoles RPG 2024…")
    fields = [metric(shape(f["geometry"])).buffer(0) for f in wfs_features("RPG.2024:parcelles_graphiques", area, progress)]
    return exclusions, unary_union(fields).intersection(area)


def land_context(area, progress=lambda message: None, metadata=None):
    try:
        result = official_land_context(area, progress)
        if metadata is not None:
            metadata.update(source="IGN BD TOPO + RPG", agricultural_year=2024)
        return result
    except (ValueError, requests.RequestException) as exc:
        progress(f"Contexte IGN indisponible ({exc}) ; essai OSM de secours.")
        exclusions, _ = osm_land_context(area, progress)
        fields = wfs_features("RPG.2024:parcelles_graphiques", area, progress)
        if metadata is not None:
            metadata.update(source="Obstacles OpenStreetMap + RPG IGN", agricultural_year=2024)
        return exclusions, rpg_overlap(fields, area)


def terrain_obstacles(area, progress, metadata=None):
    try:
        exclusions = physical_land_context(area, progress)
        source = "IGN BD TOPO"
    except (ValueError, requests.RequestException) as exc:
        progress(f"Contexte IGN indisponible ({exc}) ; essai OSM de secours.")
        exclusions, _ = osm_land_context(area, progress)
        source = "Obstacles OpenStreetMap"
    if metadata is not None:
        metadata.update(source=source)
    return exclusions


def rpg_overlap(features, area):
    return unary_union([metric(shape(feature["geometry"])).buffer(0)
                        for feature in features]).intersection(area)


def read_rpg(area, progress, metadata=None, year=2024):
    from regulatory import validated_features
    progress(f"IGN : registre parcellaire graphique {year}…")
    try:
        features = wfs_features(f"RPG.{year}:parcelles_graphiques", area, progress)
        features = validated_features(dict(type="FeatureCollection", features=features), "RPG")
        overlap = rpg_overlap(features, area)
    except (ValueError, requests.RequestException) as exc:
        progress(f"RPG {year} indisponible : {exc}")
        if metadata is not None:
            metadata.update(agricultural_year=year, rpg_status="unavailable")
        return None
    if metadata is not None:
        metadata.update(agricultural_year=year, rpg_status="checked")
    return overlap


def osm_land_context(area, progress=lambda message: None):
    """Terrain queries are separate from power and tiled to avoid giant replies."""
    x0, y0, x1, y1 = area.bounds
    exclusions, farmland = [], []
    for x in np.arange(x0, x1, 2000):
        for y in np.arange(y0, y1, 2000):
            tile = box(x, y, x + 2000, y + 2000).intersection(area)
            if tile.is_empty:
                continue
            west, south, east, north = geographic(tile).bounds
            bbox = f"{south},{west},{north},{east}"
            query = f'[out:json][timeout:30];(nwr["building"]({bbox});way["highway"]({bbox});nwr["natural"="water"]({bbox});nwr["landuse"~"^(farmland|meadow|orchard|vineyard|forest|residential|industrial)$"]({bbox}););out geom;'
            _, excluded, fields = parse_osm(overpass(query, progress))
            exclusions.append(excluded)
            farmland.append(fields)
    return unary_union(exclusions), unary_union(farmland)


def parse_osm(data):
    from electrical_data import rating_kva,current_amperes
    assets, exclusions, farmland = [], [], []
    for item in data.get("elements", []):
        tags = item.get("tags", {})
        coords = [(v["lon"], v["lat"]) for v in item.get("geometry", []) if "lon" in v]
        geometry_status = "géométrie OSM"
        if item["type"] == "node":
            geom = Point(item["lon"], item["lat"])
        elif len(coords) >= 2:
            geom = LineString(coords)
            if coords[0] == coords[-1] and len(coords) >= 4:
                from shapely.geometry import Polygon
                geom = Polygon(coords).buffer(0)
        else:
            parts = []
            for member in item.get("members", []):
                coordinates = [(p["lon"],p["lat"]) for p in member.get("geometry",[]) if "lon" in p and "lat" in p]
                if len(coordinates)>=2:
                    parts.append(LineString(coordinates))
            if parts:
                geom = unary_union(parts)
            elif item.get("center"):
                geom = Point(item["center"]["lon"],item["center"]["lat"])
                geometry_status = "centre OSM uniquement, pas le tracé"
            else:
                continue
        geom = metric(geom)
        if "power" in tags:
            assets.append({"id": f"{item['type']}/{item['id']}", "kind": "transformer" if tags.get("transformer") and tags["power"] == "pole" else tags["power"],
                           "voltage": tags.get("voltage") or tags.get("voltage:primary", "inconnue"),
                           "secondary_voltage_v": voltage_in_volts(tags.get("voltage:secondary")),
                           "primary_voltage_v": voltage_in_volts(tags.get("voltage:primary")),
                           "primary_voltage_raw": tags.get("voltage:primary"),
                           "secondary_voltage_raw": tags.get("voltage:secondary"),
                           "voltage_values_v": osm_voltage_values(tags.get("voltage")) + osm_voltage_values(tags.get("voltage:primary")) + osm_voltage_values(tags.get("voltage:secondary")),
                           "geometry_status": geometry_status,
                           "osm_tags": dict(tags), "osm_url": f"https://www.openstreetmap.org/{item['type']}/{item['id']}",
                           "planning_eligible": False if geometry_status.startswith("centre") and tags["power"] in {"line","minor_line","cable"} else True,
                           "voltage_class": tags.get("substation", "non publiée"),
                           "power_kva": rating_kva(tags.get("rating") or tags.get("rating:primary")),
                           "rating_raw": tags.get("rating") or tags.get("rating:primary"),
                           "electrical_value_source": "OpenStreetMap tags de l'objet exact",
                           "current_a": current_amperes(tags.get("max_current") or tags.get("current")),
                           "current_raw": tags.get("max_current") or tags.get("current"),
                           "cross_section": tags.get('cables:cross_section') or tags.get('cable:cross_section') or tags.get('cross_section'),
                           "name": tags.get("name"), "operator": tags.get("operator"),
                           "reference": tags.get("ref"), "circuits": tags.get("circuits"),
                           "rte_reference":tags.get("ref:FR:RTE"),
                           "cables": tags.get("cables"), "wires": tags.get("wires"),
                           "frequency_hz": tags.get("frequency"),
                           "conductor_material": tags.get("cables:material") or tags.get('cable:material') or tags.get("material"),
                           "source": "OSM", "geometry": geom})
            exclusions.append(geom.buffer(15))
        elif tags.get("landuse") in {"farmland", "meadow", "orchard", "vineyard"}:
            farmland.append(geom)
        elif "building" in tags or "highway" in tags or tags.get("natural") == "water" or tags.get("landuse") in {"forest", "residential", "industrial"}:
            exclusions.append(geom.buffer(10 if "highway" in tags else 5))
    return assets, unary_union(exclusions), unary_union(farmland)


def osm_voltage_values(value):
    """OSM volts by default; retain every voltage in multi-voltage tags."""
    if value is None:
        return []
    parts = str(value).split(";")
    common = re.search(r"(kV|V)\s*$",str(value),re.I)
    values=[]
    for part in parts:
        if common and not re.search(r"[a-zA-Z]",part):
            part=part.strip()+" "+common.group(1)
        number=voltage_in_volts(part)
        if number is not None:
            values.append(number)
    return values


def parcel_connections(parcels, assumptions):
    """Export precisely the same per-parcel distances used in the CAPEX."""
    features = []
    for parcel in parcels:
        link = parcel.get("link")
        if link is None:
            continue
        source, target = parcel["geometry"], link["target"]
        features.append({"type": "Feature", "geometry": mapping(geographic(LineString([source, target]))),
            "properties": {"parcelle": parcel["parcelle"], "ouvrage_id": link["id"],
                           "ouvrage_type": link["kind"], "source": link.get("source", "OSM"),
                           "tension_v_source": link.get("voltage", "inconnue"),
                           "puissance_assignee_source": link.get("power_kva"),
                           "distance_directe_m": link["distance_m"],
                           "longueur_budget_m": link["distance_m"] * assumptions["detour"],
                           "liaison_basse_eur": link["distance_m"] * assumptions["detour"] * assumptions["cable_low"],
                           "liaison_haute_eur": link["distance_m"] * assumptions["detour"] * assumptions["cable_high"],
                           "interface_basse_eur": assumptions["fixed_low"], "interface_haute_eur": assumptions["fixed_high"],
                           "transformateur_neuf_bas_eur": assumptions.get("transformer_low", 0),
                           "transformateur_neuf_haut_eur": assumptions.get("transformer_high", 0),
                           "statut": "Liaison directe indicative, capacité non confirmée"}})
        props = features[-1]["properties"]
        props["raccordement_bas_eur"] = (props["liaison_basse_eur"] + props["interface_basse_eur"] + props["transformateur_neuf_bas_eur"]) * (1 + assumptions["contingency"])
        props["raccordement_haut_eur"] = (props["liaison_haute_eur"] + props["interface_haute_eur"] + props["transformateur_neuf_haut_eur"]) * (1 + assumptions["contingency"])
    return features


def asset_table(assets, anchor):
    """Published characteristics; missing values must not become zero capacities."""
    rows = []
    for asset in assets:
        geom = asset["geometry"]
        p = geographic(geom.representative_point())
        rows.append({"Identifiant": asset["id"], "Type": EQUIPMENT_LABELS.get(asset["kind"], asset["kind"]), "Source": asset.get("source", "OSM"),
                     "Classe réseau": asset.get("voltage_class", "non publiée"),
                     "Tension nominale (V, donnée source)": asset.get("voltage", "inconnue"),
                     "Tension nominale (kV)": voltage_in_volts(asset.get("voltage")) / 1000 if voltage_in_volts(asset.get("voltage")) is not None else None,
                     "Libellé tension source": asset.get("voltage_raw") or asset.get("voltage", "inconnue"),
                     "Date de la donnée source": asset.get("publication_date") or asset.get("source_date"),
                     "Nom": asset.get("name"), "Gestionnaire": asset.get("operator"), "Référence": asset.get("reference"),
                     "Circuits publiés": asset.get("circuits"), "Câbles publiés": asset.get("cables"),
                     "Conducteurs par faisceau": asset.get("wires"), "Fréquence publiée (Hz)": asset.get("frequency_hz"),
                     "Matériau conducteur publié": asset.get("conductor_material"),
                     "Puissance assignée (donnée source)": str(asset["power_kva"]) if asset.get("power_kva") is not None else "non publiée",
                     "Puissance assignée (kVA)": asset.get("power_kva"),
                     "Puissance brute source": asset.get("rating_raw"),
                     "Tension secondaire (V)": asset.get("secondary_voltage_v"),
                     "Tension de conception (kV)":asset.get("design_voltage_v")/1000 if asset.get("design_voltage_v") is not None else None,
                     "Tension primaire (kV)": asset.get("primary_voltage_v") / 1000 if asset.get("primary_voltage_v") is not None else None,
                     "Tension secondaire (kV)": asset.get("secondary_voltage_v") / 1000 if asset.get("secondary_voltage_v") is not None else None,
                     "Tensions OSM (kV)": " ; ".join(f"{v/1000:g}" for v in dict.fromkeys(asset.get("voltage_values_v",[]))),
                     "Primaire OSM brut": asset.get("primary_voltage_raw"),
                     "Secondaire OSM brut": asset.get("secondary_voltage_raw"),
                     "Lien OSM": asset.get("osm_url"),
                     "Tags OSM bruts": json.dumps(asset.get("osm_tags",{}),ensure_ascii=False),
                     "Attributs OpenInfraMap":json.dumps(asset.get("openinfra_properties",{}),ensure_ascii=False),
                     "Provenance des valeurs électriques": asset.get("electrical_value_source", asset.get("source", "OSM")),
                     "Intensité (donnée source)": str(asset["current_a"]) if asset.get("current_a") is not None else "non publiée",
                     "Capacité d'injection disponible": asset.get("available_generation_mw") if asset.get("available_generation_mw") is not None else "non publiée",
                     "Capacité génération disponible (MW)": asset.get("available_generation_mw"),
                     "Génération occupée (MW)": asset.get("occupied_generation_mw"),
                     "Génération en attente (MW)": asset.get("pending_generation_mw"),
                     "Précision des coordonnées": asset.get("coordinate_precision"),
                     "Commentaires gestionnaire": asset.get("network_comments"),
                     "Lien publication": asset.get("source_url"),
                     "Utilisable en présélection": asset.get("planning_reason", "non, niveau HTB ou tension non caractérisée") if asset.get("planning_eligible") is False else "à confirmer par le gestionnaire",
                     "Distance directe (m)": round(anchor.distance(geom)),
                     "Longueur recensée (m)": round(geom.length) if asset["kind"] in {"line", "cable", "minor_line"} else None,
                     "Latitude": p.y, "Longitude": p.x})
    return pd.DataFrame(rows).sort_values("Distance directe (m)") if rows else pd.DataFrame()


def cadastral(area):
    """Prefer paginated WFS; recursively subdivide Carto responses on fallback."""
    try:
        rows = wfs_features("CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle", area, lambda _: None)
        return list({str(f.get("properties", {}).get("idu") or f.get("id") or json.dumps(f["geometry"], sort_keys=True)): f
                     for f in rows if f.get("geometry") and metric(shape(f["geometry"])).intersects(area)}.values())
    except (ValueError, requests.RequestException):
        return cadastral_carto(area)


def cadastral_carto(area):
    features = {}
    queries = 0
    def visit(tile, depth=0):
        nonlocal queries
        queries += 1
        if queries > 512:
            raise ValueError("Cadastre : budget de 512 sous-requêtes atteint, aucun résultat partiel retenu.")
        data = api("https://apicarto.ign.fr/api/cadastre/parcelle", payload={"geom": mapping(geographic(tile))})
        items = data.get("features", [])
        counts = [data.get("totalFeatures"), data.get("numberMatched")]
        truncated = any(str(n).isdigit() and int(n) > len(items) for n in counts)
        if truncated:
            if depth >= 8:
                raise ValueError("Cadastre : réponse encore tronquée après 8 subdivisions, aucune emprise validée.")
            x0, y0, x1, y1 = tile.bounds
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            for bounds in ((x0,y0,xm,ym), (xm,y0,x1,ym), (x0,ym,xm,y1), (xm,ym,x1,y1)):
                child = box(*bounds).intersection(tile)
                if not child.is_empty and child.area > .01:
                    visit(child, depth + 1)
            return
        for f in items:
            props = f.get("properties", {})
            key = props.get("idu") or f.get("id") or json.dumps(f["geometry"], sort_keys=True)
            features[str(key)] = f
    x0, y0, x1, y1 = area.bounds
    tiles = [box(x, y, x + 1000, y + 1000).intersection(area)
             for x in np.arange(x0, x1, 1000) for y in np.arange(y0, y1, 1000)]
    for tile in tiles:
        if tile.is_empty or tile.area == 0:
            continue
        visit(tile)
    return list(features.values())


def agricultural_cadastre(features, farmland, area, minimum_share=.8, basis="RPG 2024"):
    selected = []
    for feature in features:
        geom = metric(shape(feature["geometry"])).buffer(0)
        agricultural_area = geom.intersection(farmland).intersection(area).area
        observed_area = geom.intersection(area).area
        share = agricultural_area / observed_area if observed_area else 0
        if agricultural_area > 100 and share + 1e-8 >= minimum_share:
            selected.append({**feature, "properties": {**feature.get("properties", {}),
                "surface_parcelle_m2": geom.area, "surface_agricole_zone_m2": agricultural_area,
                "part_agricole_pct": share * 100, "selection_agricole": "Intersection " + basis + " dans le secteur observé",
                "nature_fiscale_cadastrale": "non publiée par ces flux"}})
    return selected


def allocate(features, area, exclusions, farmland, anchor, panel_surface, coverage, *, rpg_indicator=False):
    remaining = panel_surface / coverage
    selected, occupied = [], Point().buffer(0)
    candidates = []
    for feature in features:
        full = metric(shape(feature["geometry"])).buffer(0)
        agricultural = full.intersection(area).intersection(farmland)
        geom = agricultural.difference(exclusions).buffer(-5)
        if not geom.is_empty and geom.area > 100:
            candidates.append((geom.distance(anchor), feature, geom, full.area, agricultural.area))
    for _, feature, geom, full_area, agricultural_area in sorted(candidates, key=lambda c: c[0]):
        if rpg_indicator:
            agricultural_area=feature["properties"].get("surface_agricole_zone_m2")
        geom = geom.difference(occupied)
        if geom.is_empty:
            continue
        # Clip the actual footprint to the requested area, even across fragments.
        if geom.area > remaining:
            x0, y0, x1, y1 = geom.bounds
            lo, hi = x0, x1
            for _ in range(48):
                mid = (lo + hi) / 2
                if geom.intersection(box(x0, y0, mid, y1)).area < remaining:
                    lo = mid
                else:
                    hi = mid
            geom = geom.intersection(box(x0, y0, hi, y1))
        selected.append({"type": "Feature", "geometry": mapping(geographic(geom)),
                         "properties": {**feature["properties"], "surface_parcelle_m2": full_area,
                                        "surface_agricole_zone_m2": agricultural_area,
                                        "part_agricole_pct": 100 * agricultural_area / full.intersection(area).area if agricultural_area is not None and full.intersection(area).area else None,
                                        "emprise_m2": geom.area, "pv_m2": geom.area * coverage}})
        occupied = occupied.union(geom)
        remaining -= geom.area
        if remaining <= 0.01:
            break
    return selected, max(0, remaining * coverage)

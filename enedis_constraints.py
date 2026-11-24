"""Enedis indicative HTA/BT production zones, cached locally and queried spatially.

The public ADEME snapshot is a fallback publication, not a live Enedis API.
Never turn a qualitative regional constraint into an equipment MW rating.
"""
import gzip
import json
import sqlite3
import threading
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
import requests
from shapely.geometry import shape, mapping, Point
from shapely.ops import unary_union
from shapely import make_valid
from shapely.errors import GEOSException

OFFICIAL = 'https://observatoire.enedis.fr/sites/enedis_ote/files/processed_json/capca.json'
PAGE = 'https://observatoire.enedis.fr/services/carte-zones-contrainte-projets-enr'
COMMIT = 'f51e828088a1db5f93bdf3784b05f90fa55ea184'
SNAPSHOT_DATE = '2026-09-24T11:16:28Z'
FALLBACK = f'https://raw.githubusercontent.com/incubateur-ademe/mutafriches/{COMMIT}/apps/api/src/scripts/data/zones-contrainte-enr.geojson.gz'
VERSIONS = 'https://api.github.com/repos/incubateur-ademe/mutafriches/commits?path=apps/api/src/scripts/data/zones-contrainte-enr.geojson.gz&per_page=1'
LABELS = {'SATUREE': 'Saturée', 'EN_TENSION': 'En tension', 'FAVORABLE': 'Favorable',
          'TRES_FAVORABLE': 'Très favorable', 'ELD': 'Hors périmètre Enedis'}
LOCK = threading.RLock()
NOTE = "Prévision indicative pour la production HTA/BT. Une zone non saturée ne garantit aucune puissance disponible ; étude du gestionnaire nécessaire. Ne couvre pas le raccordement direct HTB."


def database():
    import france_pv as core
    return core.CACHE / 'enedis_constraints.sqlite3'


def download(url):
    # One attempt per source: no challenge bypass and no unbounded retry loop.
    with requests.get(url, timeout=(5, 35), stream=True) as response:
        response.raise_for_status()
        chunks=[];size=0
        for chunk in response.iter_content(262144):
            size+=len(chunk)
            if size>45_000_000:raise ValueError('Référentiel Enedis trop volumineux')
            chunks.append(chunk)
        raw=b''.join(chunks)
    if url.endswith('.gz'):
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:raw=archive.read(65_000_001)
        if len(raw)>65_000_000:raise ValueError('Référentiel décompressé trop volumineux')
    return json.loads(raw)


def normalized(data,minimum=2000):
    if not isinstance(data,dict):raise ValueError('Format Enedis invalide')
    if data.get('type')=='FeatureCollection':features=data.get('features',[])
    else:
        rows=data.get('data',{}).get('geo_status_data',{})
        features=[dict(geometry=r.get('geometry'),properties=dict(id=r.get('id',k),statut=r.get('status'))) for k,r in rows.items()]
    if len(features)<minimum:raise ValueError('Référentiel national incomplet')
    rows=[];seen=set();empty=0
    for f in features:
        if not isinstance(f,dict):raise ValueError('Objet Enedis invalide')
        props=f.get('properties') or {};identifier=str(props.get('id',''));status=props.get('statut')
        if props.get('id') is None or not identifier or identifier in seen or status not in LABELS:raise ValueError('Identifiant ou statut Enedis invalide')
        seen.add(identifier)
        geometry=f.get('geometry')
        if not geometry or not geometry.get('coordinates'):empty+=1;continue
        geom=shape(geometry)
        if geom.is_empty:empty+=1;continue
        if geom.geom_type not in {'Polygon','MultiPolygon'}:raise ValueError('Géométrie Enedis invalide')
        repaired=not geom.is_valid
        if repaired:
            geom=make_valid(geom)
            if geom.geom_type=='GeometryCollection':
                geom=unary_union([g for g in geom.geoms if g.geom_type in {'Polygon','MultiPolygon'}])
            if geom.is_empty or geom.geom_type not in {'Polygon','MultiPolygon'} or not geom.is_valid:
                raise ValueError('Géométrie Enedis irréparable')
        w,s,e,n=geom.bounds
        if not(-180<=w<=e<=180 and -90<=s<=n<=90):raise ValueError('Coordonnées Enedis invalides')
        rows.append((identifier,status,w,s,e,n,json.dumps(mapping(geom)),int(repaired)))
    if empty>3 or not rows:raise ValueError('Géométries Enedis manquantes')
    return rows,empty


def ensure_reference(progress=lambda _:None):
    path=database();path.parent.mkdir(parents=True,exist_ok=True)
    with LOCK:
        cached=path.exists()
        if cached and time.time()-path.stat().st_mtime<7*86400:return path
        cooldown=path.with_suffix('.retry')
        if cooldown.exists() and time.time()-cooldown.stat().st_mtime<3600:
            if cached:return path
            raise ValueError('Référentiel Enedis indisponible ; nouvel essai automatique après une heure')
        errors=[]
        for url,kind in [(OFFICIAL,'Enedis direct'),(FALLBACK,'Copie publique ADEME / Mutafriches')]:
            progress(f'Contraintes réseau : téléchargement {kind}…')
            try:
                version_date=SNAPSHOT_DATE;version_commit=COMMIT
                if cached and url==FALLBACK:
                    # Weekly refresh: discover a public version, then pin its URL.
                    try:
                        versions=download(VERSIONS)
                        if isinstance(versions,list) and versions:
                            commit=versions[0]['sha'];date=versions[0]['commit']['committer']['date']
                            if re.fullmatch(r'[0-9a-f]{40}',commit) and datetime.fromisoformat(date.replace('Z','+00:00'))>=datetime.fromisoformat(SNAPSHOT_DATE.replace('Z','+00:00')):
                                version_commit=commit;version_date=date;url=FALLBACK.replace(COMMIT,commit)
                    except (requests.RequestException,ValueError,KeyError,TypeError):pass
                rows,empty=normalized(download(url))
                if cached and kind!='Enedis direct':
                    with closing(sqlite3.connect(path)) as previous:
                        old=json.loads(previous.execute('SELECT value FROM metadata').fetchone()[0])
                    if old.get('snapshot_date') and old['snapshot_date']>version_date:
                        raise ValueError('Copie plus ancienne que le cache ; version précédente conservée')
                metadata=dict(source='Enedis',source_url=PAGE,download_url=url,publication=kind,
                    snapshot_date=version_date if kind!='Enedis direct' else None,
                    snapshot_commit=version_commit if kind!='Enedis direct' else None,
                    operator_update_date=None,retrieved_at=datetime.now(timezone.utc).isoformat(),
                    unlocated_zones=empty,zone_count=len(rows),repaired_zones=sum(r[-1] for r in rows),note=NOTE)
                temporary=path.with_suffix('.tmp')
                with closing(sqlite3.connect(temporary)) as db:
                    db.execute('DROP TABLE IF EXISTS zones');db.execute('DROP TABLE IF EXISTS metadata')
                    db.execute('CREATE TABLE zones (id TEXT PRIMARY KEY,status TEXT,w REAL,s REAL,e REAL,n REAL,geometry TEXT,repaired INTEGER)')
                    db.executemany('INSERT INTO zones VALUES (?,?,?,?,?,?,?,?)',rows)
                    db.execute('CREATE INDEX zone_bounds ON zones(w,e,s,n)')
                    db.execute('CREATE TABLE metadata (value TEXT)')
                    db.execute('INSERT INTO metadata VALUES (?)',(json.dumps(metadata),))
                    db.commit()
                temporary.replace(path)
                return path
            except (requests.RequestException,ValueError,sqlite3.Error,OSError,GEOSException) as exc:errors.append(f'{kind}: {exc}')
        cooldown.touch()
        if cached:
            progress('Contraintes Enedis : actualisation impossible ; dernière version complète conservée.')
            return path
        raise ValueError(' ; '.join(errors))


def context(area,progress=lambda _:None):
    import france_pv as core
    try:
        path=ensure_reference(progress)
        w,s,e,n=core.geographic(area).bounds
        with closing(sqlite3.connect(path)) as db:
            metadata=json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            rows=db.execute('SELECT id,status,geometry,repaired FROM zones WHERE w<=? AND e>=? AND s<=? AND n>=?',(e,w,n,s)).fetchall()
        features=[];saturated=[];covered=[];statuses=set()
        for identifier,status,geometry,repaired in rows:
            geom=core.metric(shape(json.loads(geometry)))
            clipped=geom.intersection(area)
            if clipped.is_empty:continue
            statuses.add(status)
            if status!='ELD':covered.append(clipped)
            if status=='SATUREE':saturated.append(clipped)
            features.append(dict(type='Feature',geometry=mapping(core.geographic(clipped)),properties=dict(id=identifier,status=status,label=LABELS[status],source='Enedis',geometry_repaired=bool(repaired))))
        saturation=unary_union(saturated)
        coverage=unary_union(covered).area/max(area.area,1)
        version=metadata.get('snapshot_date') or metadata.get('retrieved_at')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(version.replace('Z','+00:00'))).days
        return dict(status='checked' if coverage>.999 else 'partial',features=features,metadata=metadata,
            statuses=sorted(statuses),coverage_share=min(1,coverage),saturated_share=saturation.area/max(area.area,1),
            snapshot_age_days=age,stale=age>90,exclusions=saturation,note=NOTE)
    except (ValueError,OSError,sqlite3.Error,requests.RequestException,GEOSException) as exc:
        progress(f'Contraintes réseau : source non disponible ({exc}). Résultats conservés.')
        return dict(status='unavailable',features=[],metadata={},statuses=[],coverage_share=0,saturated_share=None,
                    exclusions=Point().buffer(0),note=NOTE,error=str(exc))


def annotate(assets,zones):
    """Reject saturated distribution targets; retain independently selected HTB."""
    import france_pv as core
    polygons=[(shape(f['geometry']),f['properties']['status']) for f in zones.get('features',[])]
    result=[]
    for original in assets:
        asset=dict(original);geom=core.geographic(asset['geometry'])
        statuses=sorted(set(asset.get('enedis_zone_statuses',[]))|{status for polygon,status in polygons if polygon.intersects(geom)})
        asset['enedis_zone_statuses']=statuses
        asset['enedis_zone_source']=zones.get('metadata',{})
        # A line crossing a saturated polygon cannot identify which feeder it belongs to.
        # Conservatively reject this target rather than connect inside a constrained part.
        matched=asset.get('voltage_selection_matched_v',[])
        asset['enedis_saturated_distribution']=('SATUREE' in statuses and not any(v>50000 for v in matched))
        if 'SATUREE' in statuses and matched:
            asset['voltage_selection_matched_v']=[v for v in matched if v>50000]
            if not asset['voltage_selection_matched_v']:
                asset['planning_eligible']=False
                asset['planning_reason']='Zone Enedis saturée pour la production HTA/BT (prévision indicative)'
        result.append(asset)
    return result


def public(zones):
    return {k:v for k,v in zones.items() if k!='exclusions'}

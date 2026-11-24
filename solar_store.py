"""SQLite solar aggregates, isolated by model, complete period and panel tilt."""
import json
import math
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
from contextlib import contextmanager


def valid_record(row):
    if not isinstance(row,dict):return False
    for name in ('base_yield','base_min'):
        value=row.get(name)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:return False
    for name in ('irradiance_kwh_m2','irradiance_mean_w_m2'):
        value=row.get(name)
        if value is not None and (not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):return False
    months=row.get('monthly_irradiance_kwh_m2')
    return months is None or (isinstance(months,list) and len(months)==12 and all(isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in months))


class SolarStore:
    def __init__(self,path):self.path=Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        connection=sqlite3.connect(self.path,timeout=5)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
            connection.executescript('''
            CREATE TABLE IF NOT EXISTS solar_aggregates_v1 (
              model TEXT NOT NULL, start_year INTEGER NOT NULL, end_year INTEGER NOT NULL,
              tilt REAL NOT NULL, azimuth REAL NOT NULL DEFAULT 0,
              x_key TEXT NOT NULL, y_key TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
              payload TEXT NOT NULL, fetched_at TEXT NOT NULL, origin TEXT NOT NULL,
              PRIMARY KEY(model,start_year,end_year,tilt,azimuth,x_key,y_key));
            CREATE INDEX IF NOT EXISTS solar_lookup_v1
              ON solar_aggregates_v1(model,start_year,end_year,tilt,azimuth,x,y);
            CREATE TABLE IF NOT EXISTS imported_projects_v1 (project TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL);
            ''')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put(self,model,start,end,tilt,x,y,record,origin='Open-Meteo',fetched_at=None):
        if not valid_record(record):raise ValueError('Agrégat solaire invalide ; aucune insertion en base.')
        payload=json.dumps(record,allow_nan=False)
        now=fetched_at or datetime.now(timezone.utc).isoformat()
        with self.connect() as database:
            database.execute('''INSERT INTO solar_aggregates_v1 VALUES (?,?,?,?,0,?,?,?,?,?,?,?)
              ON CONFLICT(model,start_year,end_year,tilt,azimuth,x_key,y_key) DO UPDATE SET
              payload=excluded.payload,fetched_at=excluded.fetched_at,origin=excluded.origin
              WHERE excluded.fetched_at >= solar_aggregates_v1.fetched_at''',
              (model,start,end,tilt,f'{x:.2f}',f'{y:.2f}',x,y,payload,now,origin))

    def get(self,model,start,end,tilt,x,y,maximum_distance=0):
        if not self.path.exists():return None
        scope=(model,start,end,tilt)
        with self.connect() as database:
            if maximum_distance<=0:
                rows=database.execute('''SELECT x,y,payload,fetched_at,origin FROM solar_aggregates_v1
                  WHERE model=? AND start_year=? AND end_year=? AND tilt=? AND azimuth=0 AND x_key=? AND y_key=?''',scope+(f'{x:.2f}',f'{y:.2f}')).fetchall()
            else:
                distance=min(float(maximum_distance),1000)
                rows=database.execute('''SELECT x,y,payload,fetched_at,origin FROM solar_aggregates_v1
                  WHERE model=? AND start_year=? AND end_year=? AND tilt=? AND azimuth=0
                  AND x BETWEEN ? AND ? AND y BETWEEN ? AND ? ORDER BY (x-?)*(x-?)+(y-?)*(y-?) LIMIT 32''',scope+(x-distance,x+distance,y-distance,y+distance,x,x,y,y)).fetchall()
        for sx,sy,payload,fetched,origin in rows:
            delta=math.hypot(sx-x,sy-y)
            if maximum_distance>0 and delta>maximum_distance:continue
            try:row=json.loads(payload)
            except ValueError:continue
            if valid_record(row):return {**row,'weather_source_x':sx,'weather_source_y':sy,'weather_reuse_distance_m':delta,'weather_storage':'SQLite','weather_fetched_at':fetched,'weather_origin':origin}
        return None

    def stats(self):
        if not self.path.exists():return dict(engine='SQLite',points=0,models=[],size_bytes=0)
        with self.connect() as database:
            count=database.execute('SELECT COUNT(*) FROM solar_aggregates_v1').fetchone()[0]
            models=[r[0] for r in database.execute('SELECT DISTINCT model FROM solar_aggregates_v1 ORDER BY model')]
        return dict(engine='SQLite',points=count,models=models,size_bytes=self.path.stat().st_size)


def import_projects(store,directory):
    """Offline import of completed French analysis snapshots; never calls an API."""
    from shapely.geometry import shape
    from france_pv import metric
    imported=0;skipped=0
    for path in sorted(Path(directory).glob('*/result.json'),key=lambda p:p.stat().st_mtime_ns):
        stamp=path.stat().st_mtime_ns
        with store.connect() as database:
            known=database.execute('SELECT modified_ns FROM imported_projects_v1 WHERE project=?',(path.parent.name,)).fetchone()
        if known and known[0]==stamp:continue
        try:
            data=json.loads(path.read_text(encoding='utf-8'));config=data['config'];summary=data['summary']
            if data.get('kind')!='analysis' or (config.get('country')!='France' and summary.get('terrain_country')!='France'):continue
            model=summary['weather_model'];pr=float(config['pr']);start=int(config['start']);end=int(config['end']);tilt=float(config['tilt'])
            if not model or not 0<pr<=1 or start>end:raise ValueError('Métadonnées incomplètes')
            for feature in data.get('samples',{}).get('features',[]):
                row=dict(feature['properties']);point=metric(shape(feature['geometry']))
                if point.geom_type!='Point' or point.is_empty:raise ValueError('Point solaire invalide')
                # A spatial estimate must never become a new original reference.
                if row.get('weather_reuse_distance_m',0)>.02 or row.get('distance_meteo_proxy_m',0)>.02:continue
                row['base_yield']=float(row.pop('yield'))/pr
                row['base_min']=float(row.pop('annual_min',row['base_yield']*pr))/pr
                store.put(model,start,end,tilt,point.x,point.y,row,origin='Projet '+path.parent.name,fetched_at=data.get('created_at'))
                imported+=1
            with store.connect() as database:database.execute('INSERT OR REPLACE INTO imported_projects_v1 VALUES (?,?)',(path.parent.name,stamp))
        except (OSError,ValueError,KeyError,TypeError,AttributeError):skipped+=1
    return dict(imported=imported,skipped=skipped,**store.stats())

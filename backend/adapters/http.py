from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
import csv
import io
import json
from datetime import date

from fastapi import FastAPI,HTTPException,Path as ApiPath
from fastapi.responses import FileResponse,Response,HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from countries import COUNTRIES
from backend.jobs import JobManager
from backend.models import ProjectInput,NetworkInput
from backend.economics import EconomicsInput,calculate_economics

ROOT=Path(__file__).resolve().parents[2]
Identifier=Annotated[str,ApiPath(pattern=r"^[a-f0-9]{32}$")]


def create_app(manager=None):
    jobs=manager or JobManager()
    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()
    app=FastAPI(title="Atlas Solaire — API locale",version="1.0.0",lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])
    app.state.jobs=jobs

    @app.middleware("http")
    async def fresh_frontend(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/assets/') and not request.url.path.startswith('/assets/vendor/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get("/api/health")
    def health():
        return dict(status="ok",service="atlas-solaire",version="1.0.0")

    @app.get('/api/electrical/{osm_type}/{osm_id}')
    def electrical_details(osm_type:str,osm_id:Annotated[int,ApiPath(gt=0)]):
        from electrical_data import exact_osm_details
        import requests
        if osm_type not in {'node','way','relation'}:raise HTTPException(422,'Type OSM invalide')
        try:return exact_osm_details(osm_type,osm_id)
        except (ValueError,requests.RequestException) as exc:raise HTTPException(503,f'Fiche source indisponible : {exc}') from exc

    @app.get("/api/config")
    def config():
        from connection_preferences import VOLTAGES_KV
        return dict(countries=COUNTRIES,defaults=ProjectInput().model_dump(),connection_voltages_kv=VOLTAGES_KV,first_full_year=2022,last_full_year=date.today().year-1,max_radius_km=1000,providers=["IGN / DGFiP / RPG / GPU","Enedis / RTE / Open Infrastructure Map","Open-Meteo / AROME"],coverage=dict(cadastre_automatic=["France"],territory='France métropolitaine et Corse'))

    @app.get('/api/solar-cache')
    def solar_cache():
        from solar_store import SolarStore
        from france_pv import CACHE
        import sqlite3
        try:return dict(available=True,**SolarStore(CACHE/'solar.sqlite3').stats())
        except (sqlite3.Error,OSError):return dict(available=False,engine='SQLite',points=0,models=[],size_bytes=0)

    def start(kind,config):
        try:
            identifier=jobs.start(kind,config)
        except RuntimeError as exc:
            raise HTTPException(409,str(exc)) from exc
        return dict(id=identifier,status_url=f"/api/jobs/{identifier}")

    @app.post("/api/analyses",status_code=202)
    def analysis(config:ProjectInput):
        return start("analysis",config)

    @app.post("/api/network",status_code=202)
    def network(config:NetworkInput):
        return start("network",config)

    @app.get("/api/jobs/{identifier}")
    def status(identifier:Identifier):
        data=jobs.status(identifier)
        if data is None:
            raise HTTPException(404,"Analyse introuvable.")
        return data

    @app.delete("/api/jobs/{identifier}")
    def cancel(identifier:Identifier):
        if not jobs.cancel(identifier):
            raise HTTPException(404,"Analyse introuvable.")
        return jobs.status(identifier)

    @app.get("/api/projects")
    def history():
        return jobs.history()

    @app.get("/api/projects/{identifier}")
    def project(identifier:Identifier):
        data=jobs.load(identifier)
        if data is None:
            raise HTTPException(404,"Projet introuvable.")
        return data

    @app.get("/api/projects/{identifier}/irradiance.png")
    def raster(identifier:Identifier):
        path = jobs.projects.raster(identifier)
        if path is None:
            raise HTTPException(404,"Image solaire indisponible.")
        return FileResponse(path,media_type="image/png",headers={"Cache-Control":"private, max-age=86400"})

    @app.get('/api/projects/{identifier}/economics')
    def economics(identifier:Identifier):
        return calculate_economics(project(identifier),jobs.projects.economics(identifier))

    @app.post('/api/projects/{identifier}/economics')
    def update_economics(identifier:Identifier,parameters:EconomicsInput):
        result=calculate_economics(project(identifier),parameters)
        jobs.save_economics(identifier, parameters)
        return result

    @app.get("/api/projects/{identifier}/export/{kind}")
    def export(identifier:Identifier,kind:str):
        data=project(identifier)
        if kind=='economics.csv':
            result=calculate_economics(data,jobs.projects.economics(identifier))
            if result['status']!='calculated':raise HTTPException(409,result['reason'])
            output=io.StringIO();writer=csv.writer(output,delimiter=';')
            writer.writerow(['Année','Production kWh','Recettes EUR','Exploitation EUR','Renouvellement EUR','Démantèlement EUR','Investissement EUR','Flux net EUR','Flux actualisé EUR','Cumul net EUR','Cumul actualisé EUR'])
            keys=['year','energy_kwh','revenue_eur','opex_eur','replacement_eur','decommissioning_eur','investment_eur','net_cashflow_eur','discounted_cashflow_eur','cumulative_eur','discounted_cumulative_eur']
            for row in result['central']['rows']:writer.writerow([round(row[key],4) for key in keys])
            return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="economics.csv"'})
        if kind=="report.csv":
            output=io.StringIO()
            writer=csv.writer(output,delimiter=";")
            writer.writerow(["Scénario","Production MWh/an","CAPEX bas EUR","CAPEX haut EUR","Foncier inclus"])
            for row in data.get("scenarios",[]):
                writer.writerow([row["name"],row["energy"]/1000 if not row.get("unavailable") else "",row["cost"].get("low"),row["cost"].get("high"),row.get("land_included")])
            return Response(output.getvalue().encode("utf-8-sig"),media_type="text/csv",headers={"Content-Disposition":'attachment; filename="comparatif.csv"'})
        if kind=='constraints.geojson':
            return Response(json.dumps(dict(type='FeatureCollection',features=(data.get('regulatory') or {}).get('features',[])),ensure_ascii=False),media_type='application/geo+json',headers={'Content-Disposition':'attachment; filename="constraints.geojson"'})
        if kind=='grid-constraints.geojson':
            return Response(json.dumps(dict(type='FeatureCollection',features=(data.get('grid_constraints') or {}).get('features',[])),ensure_ascii=False),media_type='application/geo+json',headers={'Content-Disposition':'attachment; filename="grid-constraints.geojson"'})
        keys={"cadastre.geojson":"cadastre","pv.geojson":"pv","network.geojson":"network","connections.geojson":"routes","samples.geojson":"samples"}
        if kind not in keys:
            raise HTTPException(404,"Export inconnu.")
        return Response(json.dumps(data.get(keys[kind],dict(type="FeatureCollection",features=[])),ensure_ascii=False),media_type="application/geo+json",headers={"Content-Disposition":f'attachment; filename="{kind}"'})

    @app.get("/")
    def index():
        html=(ROOT/"frontend"/"index.html").read_text(encoding='utf-8')
        options=''.join(f'<option value="{year}">{year}</option>' for year in range(date.today().year-1,2021,-1))
        html=html.replace('id="start" required></select>',f'id="start" required>{options}</select>')
        html=html.replace('id="end" required></select>',f'id="end" required>{options}</select>')
        return HTMLResponse(html,headers={"Cache-Control":"no-store"})

    app.mount("/assets",StaticFiles(directory=ROOT/"frontend"),name="frontend")
    return app

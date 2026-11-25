"""Real service and HTTP integration tests with isolated public-data fixtures."""
import json
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import pandas as pd
import numpy as np
from shapely.geometry import Point,mapping
from fastapi.testclient import TestClient

import france_pv as core
from backend.analysis import analyze
from backend.jobs import JobManager
from backend.main import create_app
from backend.models import ProjectInput
from backend.serialization import serialize_result


class AnalysisFixture:
    def __init__(self,*,country="France",network_error=None,context_error=None,cadastre_error=None,with_parcels=True):
        self.stack=ExitStack()
        self.stack.enter_context(patch('backend.siting.ign_elevations',side_effect=lambda x,y:np.zeros_like(x)))
        self.stack.enter_context(patch('backend.siting.regulatory_context',return_value=dict(status='partial',checks=[],features=[],exclusions=Point().buffer(0))))
        self.point=core.metric(Point(3.88,43.61))
        point=self.point
        self.assets=[dict(id="OIM/node/123",kind="transformer",source="Open Infrastructure Map",voltage="20000",primary_voltage_v=20000,secondary_voltage_v=400,geometry=Point(point.x+350,point.y))]
        self.features=[dict(type="Feature",geometry=mapping(core.geographic(point.buffer(200))),properties=dict(idu="parcel-1"))] if with_parcels else []
        def solar(points,*args,**kwargs):
            return pd.DataFrame([dict(x=p.x,y=p.y,latitude=43.61,longitude=3.88,annual_min=1400,irradiance_mean_w_m2=180,monthly_irradiance_kwh_m2=[100.]*12,**{"yield":1500}) for p in points])
        self.stack.enter_context(patch("backend.analysis.country_area",return_value=point.buffer(1000)))
        self.stack.enter_context(patch("backend.analysis.resolve_context_country",return_value=country))
        self.network=self.stack.enter_context(patch("backend.analysis.country_network",return_value=(self.assets,None,None),side_effect=network_error))
        self.cadastre=self.stack.enter_context(patch("backend.analysis.country_parcels",return_value=self.features,side_effect=cadastre_error))
        self.stack.enter_context(patch("backend.analysis.country_terrain",return_value=(Point().buffer(0),point.buffer(1000)),side_effect=context_error))
        self.stack.enter_context(patch("backend.analysis.country_price",return_value=dict(eur_m2=.65,source="Fixture officielle",department_code="34")))
        self.solar=self.stack.enter_context(patch("france_pv.solar",side_effect=solar))
        self.stack.enter_context(patch('backend.siting.grid_context',return_value=dict(status='checked',features=[],coverage_share=1,exclusions=Point().buffer(0))))
        self.config=ProjectInput(country=country,lat=43.61,lon=3.88,price_mode="Hypothèse manuelle")
    def __enter__(self):return self
    def __exit__(self,*args):return self.stack.__exit__(*args)


class ServiceTests(unittest.TestCase):
    def test_local_acquisition_is_included_once_in_parcel_capex(self):
        reference = dict(eur_m2=2, count=3, basis="terres", reference_period="2023–2025", source="DVF")
        with AnalysisFixture() as fixture, patch('backend.analysis.local_land_prices', return_value={'parcel-1': reference}) as prices:
            config = fixture.config.model_copy(update=dict(price_mode="Ventes locales comparables", price_uncertainty=0))
            result = analyze(config)
        prices.assert_called_once()
        parcels = result['parcels']
        total = sum(row['achat_estime_eur'] for row in parcels)
        self.assertAlmostEqual(total, parcels[0]['surface_parcelle_m2'] * 2)
        self.assertEqual(result['scenarios'][2]['cost']['land_low'], total)
        self.assertTrue(result['scenarios'][2]['land_included'])
        self.assertFalse(result['scenarios'][0]['land_included'])

    def test_thousand_km_scope_keeps_network_and_cadastre_queries_local(self):
        with AnalysisFixture() as fixture,patch('backend.analysis.country_area',return_value=fixture.point.buffer(600000)):
            result=analyze(fixture.config.model_copy(update=dict(radius=1000,point_budget=4,max_sectors=2)))
            for call in fixture.network.call_args_list:
                w,s,e,n=call.args[0].bounds
                self.assertLessEqual(max(e-w,n-s),8100)
            for call in fixture.cadastre.call_args_list:
                w,s,e,n=call.args[0].bounds
                self.assertLessEqual(max(e-w,n-s),2100)
            self.assertLessEqual(len(result['site_comparison']),4)  # two bounded passes

    def test_older_period_uses_one_supported_model_and_distinct_cache_keys(self):
        from france_pv import SolarEvaluator
        with AnalysisFixture() as fixture:
            config=fixture.config.model_copy(update=dict(start=2022,end=2024))
            result=analyze(config)
            self.assertEqual(result['weather_model'],'gfs_global')
            self.assertEqual(fixture.solar.call_args.kwargs['model'],'gfs_global')
        old=SolarEvaluator(2022,2024,30,.8,lambda _:None)
        recent=SolarEvaluator(2025,2025,30,.8,lambda _:None)
        self.assertEqual(old.model,'gfs_global')
        self.assertNotEqual(old.model,recent.model)

    def test_older_period_uses_one_compatible_model_and_cache_namespace(self):
        from france_pv import SolarEvaluator,solar_model
        self.assertEqual(solar_model('meteofrance_arome_france',2022),'gfs_global')
        self.assertEqual(SolarEvaluator(2023,2025,30,.8,lambda _:None).model,'gfs_global')
        self.assertEqual(ProjectInput(start=2022,end=2024).start,2022)

    def setUp(self):
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        patcher=patch("france_pv.CACHE",Path(directory.name)/"cache");patcher.start();self.addCleanup(patcher.stop)
        self.directory=Path(directory.name)

    def test_service_keeps_all_three_scenarios_without_any_ui_framework(self):
        with AnalysisFixture() as fixture:
            result=analyze(fixture.config)
            payload=serialize_result(result,fixture.config,"a"*32,self.directory/"project")
        self.assertEqual(len(payload["scenarios"]),3)
        self.assertGreater(payload["summary"]["placed_m2"],9900)
        self.assertEqual(payload["collector"]["connections"],1)
        self.assertEqual(payload["network"]["features"][0]["properties"]["primary_voltage_v"],20000)
        self.assertTrue((self.directory/"project"/"irradiance.png").read_bytes().startswith(b"\x89PNG"))
        json.dumps(payload,allow_nan=False)

    def test_network_outage_keeps_solar_and_parcel_results(self):
        with AnalysisFixture(network_error=ValueError("source unavailable")) as fixture:
            result=analyze(fixture.config)
        self.assertTrue(result["scenarios"][1]["unavailable"])
        self.assertFalse(result["scenarios"][2]["unavailable"])
        self.assertIsNone(result["scenarios"][2]["cost"]["high"])
        self.assertLess(result["missing"],1)

    def test_terrain_failure_does_not_become_a_false_surface_deficit(self):
        with AnalysisFixture(context_error=ValueError("terrain unavailable")) as fixture:
            result=analyze(fixture.config)
        self.assertIsNone(result["missing"])
        self.assertTrue(result["scenarios"][2]["unavailable"])

    def test_steep_and_unverified_parcels_are_never_proposed(self):
        for grade, decision in ((.15, 'accepted'), (.16, 'insufficient_land'), (np.nan, 'source_unavailable')):
            with self.subTest(grade=grade), AnalysisFixture() as fixture, patch(
                'backend.siting.ign_elevations', side_effect=lambda x,y:(x-x.min())*grade,
            ):
                result=analyze(fixture.config)
                self.assertEqual(result['site_comparison'][0]['decision'],decision)
                self.assertEqual(result['scenarios'][2]['unavailable'],grade != .15)

    def test_cadastre_failure_does_not_become_a_false_empty_search(self):
        with AnalysisFixture(cadastre_error=ValueError("cadastre unavailable")) as fixture:
            result=analyze(fixture.config)
        self.assertTrue(any("Cadastre indisponible" in w for w in result["warnings"]))
        self.assertFalse(any("Aucune parcelle cadastrale" in w for w in result["warnings"]))

    def test_empty_cadastre_has_no_invented_missing_surface(self):
        with AnalysisFixture(with_parcels=False) as fixture:
            result=analyze(fixture.config)
        self.assertIsNone(result["missing"])

    def test_zero_usable_land_is_not_a_chosen_or_available_implementation(self):
        with AnalysisFixture() as fixture,patch('backend.analysis.country_terrain',return_value=(fixture.point.buffer(2000),fixture.point.buffer(2000))):
            result=analyze(fixture.config)
        self.assertTrue(result['scenarios'][2]['unavailable'])
        self.assertEqual(result['missing'],fixture.config.surface)
        self.assertFalse(any(row['chosen'] for row in result['site_comparison']))
        self.assertEqual(result['site_comparison'][0]['status'],'no_usable_land')


    def test_repeat_analysis_reuses_persistent_weather(self):
        with AnalysisFixture() as fixture:
            analyze(fixture.config)
            second=analyze(fixture.config.model_copy(update=dict(surface=20000,pr=.9)))
            self.assertEqual(fixture.solar.call_count,1)
        self.assertEqual(second["weather_cache_hits"],1)



class APITests(unittest.TestCase):
    def setUp(self):
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.directory=Path(directory.name)
        patcher=patch("france_pv.CACHE",self.directory/"cache");patcher.start();self.addCleanup(patcher.stop)
        self.manager=JobManager(self.directory/"projects")
        self.client=TestClient(create_app(self.manager))
        self.client.__enter__()
        self.addCleanup(self.manager.executor.shutdown,wait=True)
        self.addCleanup(self.client.__exit__,None,None,None)

    def wait(self,identifier):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            response=self.client.get(f"/api/jobs/{identifier}").json()
            if response["status"] in {"done","failed","cancelled"}:return response
            time.sleep(.02)
        self.fail("Job did not finish")

    def test_health_configuration_and_local_frontend_are_served(self):
        self.assertEqual(self.client.get("/api/health").json()["status"],"ok")
        self.assertEqual(list(self.client.get("/api/config").json()["countries"]),["France"])
        html=self.client.get("/")
        self.assertIn('id="map"',html.text)
        for identifier in ('start','end'):
            self.assertIn(f'id="{identifier}" required><option value="',html.text)
        self.assertIn('<option value="2022">2022</option>',html.text)
        self.assertEqual(html.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get('/assets/app.js').headers['cache-control'],'no-store')
        self.assertNotIn("streamlit",html.text.lower())
        self.assertEqual(self.client.get("/assets/app.js").status_code,200)
        self.assertEqual(self.client.get("/assets/vendor/leaflet.js").status_code,200)

    def test_invalid_input_is_rejected_before_any_external_call(self):
        with patch("backend.jobs.analyze") as service:
            self.assertEqual(self.client.post("/api/analyses",json=dict(surface=-1)).status_code,422)
            self.assertEqual(self.client.post("/api/analyses",json=dict(country="Atlantide")).status_code,422)
            self.assertEqual(self.client.post("/api/analyses",json=dict(start=2026,end=2025)).status_code,422)
            self.assertEqual(self.client.post("/api/analyses",json=dict(pv_low=2000,pv_high=1000)).status_code,422)
        service.assert_not_called()

    def test_solar_database_status_is_local_and_reports_real_entries(self):
        from solar_store import SolarStore
        with TemporaryDirectory() as directory,patch('france_pv.CACHE',Path(directory)):
            response=self.client.get('/api/solar-cache')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['points'],0)
            SolarStore(Path(directory)/'solar.sqlite3').put('gfs_global',2023,2023,30,0,0,dict(base_yield=1000,base_min=1000))
            self.assertEqual(self.client.get('/api/solar-cache').json()['points'],1)

    def test_async_result_history_raster_and_exports(self):
        with AnalysisFixture() as fixture:
            response=self.client.post("/api/analyses",json=fixture.config.model_dump())
            self.assertEqual(response.status_code,202)
            identifier=response.json()["id"]
            status=self.wait(identifier)
        self.assertEqual(status["status"],"done",status)
        self.assertEqual(status['progress']['completed_steps'],3)
        self.assertTrue(all(step['state']=='done' for step in status['progress']['steps']))
        result=self.client.get(f"/api/projects/{identifier}").json()
        self.assertEqual(result["kind"],"analysis")
        self.assertEqual(len(self.client.get("/api/projects").json()),1)
        self.assertEqual(self.client.get(f"/api/projects/{identifier}/irradiance.png").headers["content-type"],"image/png")
        self.assertEqual(self.client.get(f"/api/projects/{identifier}/export/pv.geojson").json()["type"],"FeatureCollection")
        self.assertIn("CAPEX",self.client.get(f"/api/projects/{identifier}/export/report.csv").text)
        self.assertEqual(self.client.get(f"/api/projects/{identifier}/export/unknown").status_code,404)

    def test_second_job_is_refused_and_cancellation_is_cooperative(self):
        started=threading.Event();release=threading.Event()
        def blocking(config,progress):
            started.set();release.wait(5);progress("Après attente")
        with patch("backend.jobs.analyze",side_effect=blocking):
            identifier=self.client.post("/api/analyses",json={}).json()["id"]
            self.assertTrue(started.wait(2))
            self.assertEqual(self.client.post("/api/analyses",json={}).status_code,409)
            self.assertEqual(self.client.delete(f"/api/jobs/{identifier}").status_code,200)
            release.set();status=self.wait(identifier)
        self.assertEqual(status["status"],"cancelled")

    def test_missing_projects_and_path_traversal_do_not_expose_files(self):
        self.assertEqual(self.client.get('/api/projects/'+"a"*32).status_code,404)
        self.assertEqual(self.client.get('/api/projects/not-a-project').status_code,422)
        self.assertEqual(self.client.get('/api/jobs/'+"a"*32).status_code,404)

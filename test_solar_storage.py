import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import pandas as pd
from shapely.geometry import Point,mapping
import france_pv as core
from solar_store import SolarStore,import_projects


class SolarStorageTests(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        patcher=patch('france_pv.CACHE',self.root/'cache');patcher.start();self.addCleanup(patcher.stop)
        self.store=SolarStore(core.CACHE/'solar.sqlite3')
        self.point=core.metric(Point(3.88,43.61))
        self.record=dict(base_yield=1000.,base_min=1000.,irradiance_kwh_m2=1000.,monthly_irradiance_kwh_m2=[1000/12]*12)

    def put(self,**kwargs):
        self.store.put(core.METEO_MODEL,2025,2025,30,self.point.x,self.point.y,self.record,**kwargs)

    def test_persists_and_isolates_model_period_and_tilt(self):
        self.put()
        reopened=SolarStore(self.store.path)
        self.assertEqual(reopened.get(core.METEO_MODEL,2025,2025,30,self.point.x,self.point.y)['base_yield'],1000)
        for model,start,end,tilt in [('gfs_global',2025,2025,30),(core.METEO_MODEL,2024,2025,30),(core.METEO_MODEL,2025,2025,35)]:
            self.assertIsNone(reopened.get(model,start,end,tilt,self.point.x,self.point.y))

    def test_nearest_query_respects_true_distance_and_requested_scope(self):
        self.put()
        row=self.store.get(core.METEO_MODEL,2025,2025,30,self.point.x+300,self.point.y+300,500)
        self.assertAlmostEqual(row['weather_reuse_distance_m'],(300**2+300**2)**.5)
        self.assertIsNone(self.store.get(core.METEO_MODEL,2025,2025,30,self.point.x+400,self.point.y+400,500))
        self.assertIsNone(self.store.get('gfs_global',2025,2025,30,self.point.x+100,self.point.y,500))

    def test_invalid_and_nonfinite_aggregates_are_not_stored(self):
        for bad in [{**self.record,'base_yield':float('nan')},{**self.record,'base_min':-1},{**self.record,'monthly_irradiance_kwh_m2':[1,2]}]:
            with self.assertRaises(ValueError):self.store.put(core.METEO_MODEL,2025,2025,30,0,0,bad)
        self.assertEqual(self.store.stats()['points'],0)

    def test_corrupt_payload_is_ignored_and_latest_source_wins(self):
        self.put(fetched_at='2026-01-02T00:00:00+00:00')
        self.store.put(core.METEO_MODEL,2025,2025,30,self.point.x,self.point.y,{**self.record,'base_yield':900},fetched_at='2026-01-01T00:00:00+00:00')
        self.assertEqual(self.store.get(core.METEO_MODEL,2025,2025,30,self.point.x,self.point.y)['base_yield'],1000)
        with self.store.connect() as database:database.execute("UPDATE solar_aggregates_v1 SET payload='{}'")
        self.assertIsNone(self.store.get(core.METEO_MODEL,2025,2025,30,self.point.x,self.point.y))

    def fake_solar(self,points,start,end,tilt,pr,*args,**kwargs):
        return pd.DataFrame([dict(x=p.x,y=p.y,annual_min=1000*pr,irradiance_kwh_m2=1000.,**{'yield':1000*pr}) for p in points])

    def evaluator(self,pr=.8,reuse=500):
        return core.SolarEvaluator(2025,2025,30,pr,lambda _:None,maximum=8,persistent=True,reuse_distance_m=reuse)

    def test_reused_estimate_is_not_saved_as_an_original_and_pr_is_recomputed(self):
        p=self.point
        with patch('france_pv.solar',side_effect=self.fake_solar) as fetch:
            self.evaluator()([p])
            second=self.evaluator(pr=.9)
            row=second([Point(p.x+300,p.y)]).iloc[0]
            self.assertAlmostEqual(row['yield'],900)
            self.assertAlmostEqual(row['weather_reuse_distance_m'],300)
            self.assertEqual(second.db_hits,1)
            self.assertEqual(second.nearby_hits,1)
            self.evaluator()([Point(p.x+600,p.y)])
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(self.store.stats()['points'],2)

    def test_exact_only_mode_does_not_reuse_nearby_point(self):
        p=self.point
        with patch('france_pv.solar',side_effect=self.fake_solar) as fetch:
            self.evaluator()([p])
            second=self.evaluator(reuse=0)
            second([Point(p.x+100,p.y)])
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(second.db_hits,0)

    def test_legacy_json_is_migrated_without_http(self):
        evaluator=self.evaluator()
        path=evaluator.cache_path(self.point);path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.record),encoding='utf-8')
        with patch('france_pv.solar',side_effect=AssertionError('No weather request expected')):
            row=evaluator([self.point]).iloc[0]
        self.assertAlmostEqual(row['yield'],800)
        self.assertEqual(self.store.stats()['points'],1)

    def test_sqlite_failure_keeps_file_cache_and_analysis_available(self):
        self.store.path.mkdir(parents=True)
        with patch('france_pv.solar',side_effect=self.fake_solar) as fetch:
            first=self.evaluator();first([self.point])
            second=self.evaluator();result=second([self.point])
        self.assertEqual(fetch.call_count,1)
        self.assertAlmostEqual(result.iloc[0]['yield'],800)
        self.assertTrue(first.cache_path(self.point).exists())

    def test_project_import_is_offline_idempotent_and_skips_estimates(self):
        projects=self.root/'projects'/'example';projects.mkdir(parents=True)
        point=Point(3.88,43.61)
        properties=dict(annual_min=800,irradiance_kwh_m2=1000.,**{'yield':800})
        data=dict(kind='analysis',created_at='2026-01-01T00:00:00+00:00',config=dict(country='France',start=2025,end=2025,tilt=30,pr=.8),summary=dict(weather_model=core.METEO_MODEL),samples=dict(type='FeatureCollection',features=[dict(geometry=mapping(point),properties=properties),dict(geometry=mapping(Point(3.881,43.61)),properties={**properties,'weather_reuse_distance_m':100})]))
        (projects/'result.json').write_text(json.dumps(data),encoding='utf-8')
        with patch('france_pv.requests.get',side_effect=AssertionError('Offline migration')):
            first=import_projects(self.store,self.root/'projects')
            second=import_projects(self.store,self.root/'projects')
        self.assertEqual(first['imported'],1)
        self.assertEqual(second['imported'],0)
        self.assertEqual(self.store.stats()['points'],1)


if __name__=='__main__':unittest.main()

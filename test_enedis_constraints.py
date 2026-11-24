"""Offline provider, cache and planning regression tests."""
import json
import os
import time
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import requests
from shapely.geometry import Point,box,mapping
import france_pv as core
import enedis_constraints as enedis
from connection_preferences import apply_voltage_preferences


def zones(status='SATUREE',geometry=None):
    return dict(type='FeatureCollection',features=[dict(type='Feature',geometry=mapping(geometry or box(3,44,3.01,44.01)),properties=dict(id='one',statut=status))])


class EnedisTests(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        patcher=patch('france_pv.CACHE',Path(self.temporary.name));patcher.start();self.addCleanup(patcher.stop)
        self.point=core.metric(Point(3.005,44.005));self.area=self.point.buffer(100)

    def install(self,data):
        real=enedis.normalized
        with patch.object(enedis,'download',return_value=data),patch.object(enedis,'normalized',side_effect=lambda d:real(d,minimum=1)):
            return enedis.ensure_reference()

    def test_official_refusal_uses_versioned_publication_and_cache_survives_restart(self):
        real=enedis.normalized
        with patch.object(enedis,'download',side_effect=[requests.HTTPError('403'),zones()]) as fetch,patch.object(enedis,'normalized',side_effect=lambda d:real(d,minimum=1)):
            path=enedis.ensure_reference()
            self.assertEqual(fetch.call_count,2)
            self.assertIn(enedis.COMMIT,fetch.call_args.args[0])
        with patch.object(enedis,'download',side_effect=AssertionError('Offline')):
            result=enedis.context(self.area)
        self.assertEqual(result['metadata']['snapshot_date'],enedis.SNAPSHOT_DATE)
        self.assertIsNone(result['metadata']['operator_update_date'])
        self.assertGreater(result['saturated_share'],.999)
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM zones').fetchone()[0],1)

    def test_tension_is_not_saturation_and_eld_is_not_confirmed_coverage(self):
        for status in ['EN_TENSION','ELD']:
            path=self.install(zones(status))
            result=enedis.context(self.area)
            self.assertTrue(result['exclusions'].is_empty)
            self.assertEqual(result['coverage_share'],0 if status=='ELD' else 1)
            path.unlink()

    def test_missing_zone_is_unknown_not_favourable(self):
        self.install(zones())
        result=enedis.context(core.metric(Point(2,45)).buffer(100))
        self.assertEqual(result['coverage_share'],0)
        self.assertEqual(result['statuses'],[])
        self.assertEqual(result['status'],'partial')

    def test_truncated_or_unknown_reference_never_replaces_cache(self):
        with self.assertRaisesRegex(ValueError,'incomplet'):enedis.normalized(zones())
        with self.assertRaisesRegex(ValueError,'statut'):enedis.normalized(zones('UNKNOWN'),minimum=1)
        with patch.object(enedis,'download',return_value=zones()):
            with self.assertRaises(ValueError):enedis.ensure_reference()
        self.assertFalse(enedis.database().exists())
        with patch.object(enedis,'download') as fetch:
            self.assertEqual(enedis.context(self.area)['status'],'unavailable')
            fetch.assert_not_called()

    def test_invalid_polygon_repair_is_recorded(self):
        data=zones();data['features'][0]['geometry']=dict(type='Polygon',coordinates=[[(3,44),(3.01,44.01),(3.01,44),(3,44.01),(3,44)]])
        rows,_=enedis.normalized(data,minimum=1)
        self.assertEqual(rows[0][-1],1)

    def test_failed_refresh_keeps_complete_reference_and_persists_cooldown(self):
        path=self.install(zones())
        old=time.time()-8*86400;os.utime(path,(old,old))
        with patch.object(enedis,'download',side_effect=requests.ReadTimeout('offline')):
            self.assertEqual(enedis.ensure_reference(),path)
        self.assertTrue(path.with_suffix('.retry').exists())
        with patch.object(enedis,'download') as fetch:
            self.assertGreater(enedis.context(self.area)['saturated_share'],.99)
            fetch.assert_not_called()

    def test_sat_target_filtered_but_htb_kept_and_ratings_never_invented(self):
        self.install(zones())
        result=enedis.context(self.area)
        assets=apply_voltage_preferences([dict(id='hta',kind='line',voltage='20000',geometry=self.point),dict(id='htb',kind='line',voltage='63000',geometry=self.point)],[20,63])
        assets=enedis.annotate(assets,result)
        self.assertFalse(assets[0]['planning_eligible'])
        self.assertTrue(assets[1]['planning_eligible'])
        self.assertNotIn('available_generation_mw',assets[0])
        # Reapplying UI preferences must not resurrect the saturated HTA target.
        again=apply_voltage_preferences(assets,[20,63])
        self.assertEqual(core.connection(self.point,again)['id'],'htb')
        outside=dict(id='outside',kind='line',voltage='20000',geometry=core.metric(Point(4,45)))
        self.assertFalse(enedis.annotate([outside],result)[0]['enedis_zone_statuses'])

    def test_actual_geoshape_is_requested_and_nominal_values_stay_unknown(self):
        row=dict(_id='official',_geoshape=mapping(core.geographic(self.point)))
        with patch('france_pv.api',side_effect=[dict(results=[row]),dict(results=[]),dict(results=[])]) as fetch:
            assets=core.enedis_assets(self.area)
        self.assertEqual(len(assets),1)
        self.assertIn('_geoshape',fetch.call_args_list[0].args[1]['select'])
        self.assertIsNone(assets[0]['voltage'])
        self.assertIsNone(assets[0]['power_kva'])
        self.assertIn('opendata.enedis.fr',assets[0]['source_url'])

    def test_tension_and_eld_are_not_converted_into_saturation(self):
        for status in ('EN_TENSION','FAVORABLE','TRES_FAVORABLE','ELD'):
            with self.subTest(status=status):
                enedis.database().unlink(missing_ok=True)
                self.install(zones(status))
                result=enedis.context(self.area)
                self.assertTrue(result['exclusions'].is_empty)
                self.assertEqual(result['coverage_share']==0,status=='ELD')
                assets=apply_voltage_preferences([dict(id='x',kind='transformer',voltage='20000',geometry=self.point)],[20])
                self.assertTrue(enedis.annotate(assets,result)[0]['planning_eligible'])


if __name__=='__main__':unittest.main()

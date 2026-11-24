import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock,patch

import mapbox_vector_tile
import numpy as np
import pandas as pd
from shapely.geometry import Point,mapping,box,LineString

import france_pv as core
from countries import COUNTRIES,electrical_network,parcels,land_reference
from openinfra_network import decode_tile,read_tile,tile_indices,network_assets
from map_ui import irradiance_raster


class OpeninfraTests(unittest.TestCase):
    def setUp(self):
        cache=TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        patcher=patch("france_pv.CACHE",Path(cache.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tile(self):
        feature=dict(id=123,geometry=mapping(Point(2048,2048)),properties=dict(is_node=True,voltage_primary="20",voltage_secondary="0.4",rating="630000"))
        return mapbox_vector_tile.encode(dict(name="power_transformer",features=[feature]),default_options={"y_coord_down":True})

    def test_tile_voltage_kv_is_converted_to_volts_and_rating_to_kva(self):
        assets=decode_tile(self.tile(),14,8470,5838)
        self.assertEqual(len(assets),1)
        asset=assets[0]
        self.assertEqual(asset["id"],"OIM/node/123")
        self.assertEqual(asset["primary_voltage_v"],20000)
        self.assertEqual(asset["secondary_voltage_v"],400)
        self.assertEqual(asset["power_kva"],630)
        location=core.geographic(asset["geometry"])
        self.assertTrue(5<location.x<7 and 44<location.y<48)

    def test_tile_cache_is_used_even_during_server_cooldown(self):
        response=Mock(status_code=200,content=self.tile())
        with patch("openinfra_network.requests.get",return_value=response) as get:
            first,cached=read_tile((14,8470,5838),lambda _:None)
            self.assertFalse(cached)
            (core.CACHE/"openinfra_tiles"/"cooldown.json").write_text(json.dumps(dict(until=time.time()+3600)))
            second,cached=read_tile((14,8470,5838),lambda _:None)
            self.assertTrue(cached)
        self.assertEqual(get.call_count,1)
        self.assertEqual(first[0]["id"],second[0]["id"])

    def test_extra_voltage_fields_and_invalid_values_do_not_lose_the_tile(self):
        feature=dict(id=123,geometry=mapping(Point(2048,2048)),properties=dict(is_node=True,voltage='unknown',voltage_5=63,voltage_secondary=.4))
        data=mapbox_vector_tile.encode(dict(name='power_transformer',features=[feature]),default_options={'y_coord_down':True})
        asset=decode_tile(data,14,8470,5838)[0]
        self.assertIn(63000,asset['voltage_values_v'])
        self.assertEqual(asset['secondary_voltage_v'],400)
        self.assertTrue(asset['openinfra_url'].startswith('https://openinframap.org/#16/'))

    def test_fragments_keep_electrical_values_missing_from_the_first_tile(self):
        a=dict(id='OIM/way/1',kind='line',geometry=LineString([(0,0),(10,0)]),voltage='inconnue',voltage_values_v=[],power_kva=None)
        b={**a,'geometry':LineString([(5,0),(20,0)]),'voltage':'20000','voltage_values_v':[20000],'power_kva':630}
        with patch('openinfra_network.tile_indices',return_value=[(14,1,1),(14,2,1)]),patch('openinfra_network.read_tile',side_effect=[([a],False),([b],True)]):
            asset=network_assets(box(-1,-1,21,1))[0]
        self.assertEqual(asset['voltage'],'20000')
        self.assertEqual(asset['power_kva'],630)

    def test_429_is_bounded_and_persisted(self):
        response=Mock(status_code=429,headers={"Retry-After":"3600"})
        with patch("openinfra_network.requests.get",return_value=response) as get,patch("france_pv.wait_until") as wait:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    read_tile((14,1,1),lambda _:None)
        self.assertEqual(get.call_count,1)
        wait.assert_not_called()

    def test_fragments_from_adjacent_tiles_do_not_double_count_the_line(self):
        a=dict(id="OIM/way/1",kind="line",geometry=LineString([(0,0),(10,0)]))
        b=dict(id="OIM/way/1",kind="line",geometry=LineString([(5,0),(20,0)]))
        with patch("openinfra_network.tile_indices",return_value=[(14,1,1),(14,2,1)]),patch("openinfra_network.read_tile",side_effect=[([a],False),([b],True)]):
            result=network_assets(box(-1,-1,21,1))
        self.assertEqual(len(result),1)
        self.assertAlmostEqual(result[0]["geometry"].length,20)



    def test_tile_budget_never_silently_drops_transformers_by_lowering_zoom(self):
        with self.assertRaisesRegex(ValueError,"réduire"):
            tile_indices(core.metric(Point(13.4,52.5)).buffer(100000))

    def test_weather_points_survive_relaunch_and_reuse_irradiance_for_new_pr(self):
        point=core.metric(Point(13.4,52.5))
        def fake(points,*args,**kwargs):
            return pd.DataFrame([dict(x=p.x,y=p.y,annual_min=720,irradiance_kwh_m2=1000,irradiance_mean_w_m2=114,**{"yield":800}) for p in points])
        with patch("france_pv.solar",side_effect=fake) as fetch:
            first=core.SolarEvaluator(2025,2025,30,.8,lambda _:None,maximum=4,model="ecmwf_ifs",persistent=True)
            first([point])
            second=core.SolarEvaluator(2025,2025,30,.9,lambda _:None,maximum=4,model="ecmwf_ifs",persistent=True)
            result=second([point])
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(second.cache_hits,1)
        self.assertAlmostEqual(result.iloc[0]["yield"],900)
        self.assertEqual(result.iloc[0]["irradiance_mean_w_m2"],114)

    def test_weather_render_cache_is_reused(self):
        point=core.metric(Point(13.4,52.5))
        samples=pd.DataFrame([dict(x=point.x,y=point.y,irradiance_mean_w_m2=150)])
        first=irradiance_raster(point.buffer(1000),samples,size=20,cache=True)
        with patch("map_ui.intersects_xy",side_effect=AssertionError("Raster already cached")):
            second=irradiance_raster(point.buffer(1000),samples,size=20,cache=True)
        self.assertTrue(np.array_equal(first[0],second[0]))



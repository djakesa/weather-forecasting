import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from shapely.geometry import Point
import france_pv as core
from electrical_data import enrich_transformers,electrical_summary
from map_ui import irradiance_raster,mean_irradiance_values


class OSMValuesDisplayTests(unittest.TestCase):
    def test_known_osm_values_are_shown_before_nearer_unknown_posts(self):
        origin=core.metric(Point(6.14,45.85))
        assets=[dict(id="Enedis/unknown",kind="transformer",voltage="inconnue",geometry=origin),
                dict(id="node/1",kind="transformer",voltage="20000",primary_voltage_v=20000,geometry=Point(origin.x+200,origin.y))]
        result=electrical_summary(core.asset_table(assets,origin))
        self.assertEqual(result.iloc[0]["Identifiant"],"node/1")
        self.assertEqual(result.iloc[0]["Tension primaire (kV)"],20)

    def test_hourly_series_is_averaged_including_night_hours(self):
        times=pd.date_range("2025-01-01","2026-01-01",freq="h",inclusive="left").strftime("%Y-%m-%dT%H:%M").tolist()
        response={"hourly":{"time":times,"global_tilted_irradiance":[0,200]*(len(times)//2)}}
        with patch("france_pv.api",return_value=response):
            frame=core.solar([core.metric(Point(6.14,45.85))],2025,2025,30,.8)
        self.assertEqual(frame.iloc[0]["irradiance_mean_w_m2"],100)
        self.assertAlmostEqual(frame.iloc[0]["irradiance_kwh_m2"],876)

    def test_full_area_enrichment_includes_lines_without_initial_transformers(self):
        area=core.metric(Point(6.14,45.85)).buffer(500)
        asset=dict(id="way/9",kind="line",voltage="20000",voltage_values_v=[20000],geometry=area.centroid)
        with patch("france_pv.electrical_osm",return_value=[asset]) as fetch:
            result=enrich_transformers([],area)
        self.assertEqual(result,[asset])
        self.assertTrue(fetch.call_args.args[0].equals(area))

    def test_query_contains_user_voltage_filters_in_selected_bbox(self):
        area=core.metric(Point(-3.718,40.405)).buffer(500)
        with patch("france_pv.overpass",return_value={"elements":[]}) as fetch:
            core.electrical_osm(area)
        query=fetch.call_args.args[0]
        for tag in ('["voltage"]','["voltage:primary"]','["voltage:secondary"]','out center geom;'):
            self.assertIn(tag,query)
        self.assertNotIn("area.pays",query)

    def test_primary_secondary_multivoltage_and_raw_tags_survive_export(self):
        data={"elements":[dict(type="node",id=1,lon=6.14,lat=45.85,tags={"power":"transformer","voltage:primary":"20000","voltage:secondary":"400"}),
            dict(type="way",id=2,tags={"power":"cable","voltage":"220000;132000"},geometry=[dict(lon=-3.718,lat=40.405),dict(lon=-3.719,lat=40.406)])]}
        assets,_,_=core.parse_osm(data)
        table=core.asset_table(assets,assets[0]["geometry"])
        transformer=table[table["Identifiant"]=="node/1"].iloc[0]
        cable=table[table["Identifiant"]=="way/2"].iloc[0]
        self.assertEqual(transformer["Tension primaire (kV)"],20)
        self.assertEqual(transformer["Tension secondaire (kV)"],.4)
        self.assertEqual(cable["Tensions OSM (kV)"],"220 ; 132")
        self.assertIn("voltage:primary",transformer["Tags OSM bruts"])
        self.assertEqual(core.osm_voltage_values("63;20kV"),[63000,20000])

    def test_relation_member_geometry_and_center_are_not_dropped(self):
        data={"elements":[dict(type="relation",id=1,tags={"power":"line","voltage":"63000"},members=[dict(geometry=[dict(lon=6.14,lat=45.85),dict(lon=6.15,lat=45.86)])]),
            dict(type="relation",id=2,tags={"power":"transformer","voltage:secondary":"400"},center=dict(lon=6.14,lat=45.85))]}
        assets,_,_=core.parse_osm(data)
        self.assertEqual(len(assets),2)
        self.assertEqual(assets[1]["secondary_voltage_v"],400)
        self.assertIn("centre",assets[1]["geometry_status"])

    def test_regular_raster_uses_temporal_mean_without_extra_api_calls(self):
        origin=core.metric(Point(6.14,45.85))
        samples=pd.DataFrame([dict(x=origin.x-200,y=origin.y,irradiance_mean_w_m2=100),dict(x=origin.x+200,y=origin.y,irradiance_mean_w_m2=200)])
        with patch("france_pv.solar",side_effect=AssertionError("No extra weather request")):
            raster,mask,bounds,means=irradiance_raster(origin.buffer(500),samples,size=40)
        self.assertEqual(raster.shape,(40,40))
        self.assertTrue(np.all(raster[mask]>=100))
        self.assertTrue(np.all(raster[mask]<=200))
        self.assertGreater(len(np.unique(raster[mask].round(1))),20)
        self.assertTrue(np.array_equal(means,[100,200]))

    def test_constant_samples_remain_constant_and_old_energy_cache_is_converted(self):
        origin=core.metric(Point(6.14,45.85))
        samples=pd.DataFrame([dict(x=origin.x,y=origin.y,irradiance_mean_w_m2=180)])
        raster,mask,_,_=irradiance_raster(origin.buffer(500),samples,size=20)
        self.assertTrue(np.allclose(raster[mask],180))
        old=pd.DataFrame([dict(irradiance_kwh_m2=1800)])
        self.assertAlmostEqual(mean_irradiance_values(old,mean_year_hours=8784)[0],1800000/8784)

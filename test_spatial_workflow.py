import unittest
from unittest.mock import patch
import pandas as pd
from shapely.geometry import Point, box, shape, MultiPolygon, mapping
from shapely.ops import unary_union
import france_pv as core
from spatial_sampling import sample_solar,scattered_points,network_proxy_candidates
from map_ui import irradiance_cells
from shared_grid import shared_connection_plan
from field_routing import split_fields


class SpatialWorkflowTests(unittest.TestCase):


    def test_importing_a_plate_does_not_unlock_zero_generation_capacity(self):
        from electrical_data import apply_verified_inventory
        asset=dict(id="P",kind="substation",voltage="20000",available_generation_mw=0,planning_eligible=False)
        assets,_=apply_verified_inventory([asset],"asset_id,source,power_kva\nP,plaque,630\n")
        self.assertEqual(assets[0]["power_kva"],630)
        self.assertFalse(assets[0]["planning_eligible"])

    def test_scattered_sampling_is_bounded_spaced_and_reproducible(self):
        area=Point(0,0).buffer(20000)
        points=scattered_points(area,16,2500)
        self.assertEqual(points,scattered_points(area,16,2500))
        self.assertLessEqual(len(points),16)
        self.assertTrue(all(area.covers(p) for p in points))
        self.assertTrue(all(a.distance(b)>=2500 for i,a in enumerate(points) for b in points[i+1:]))

    def test_no_grid_and_budget_is_never_exceeded(self):
        def solar(points,*args,**kwargs):
            return pd.DataFrame([dict(x=p.x,y=p.y,**{"yield":1400+p.x/10000}) for p in points])
        evaluator=core.SolarEvaluator(2025,2025,30,.8,lambda _:None,maximum=4)
        with patch("france_pv.solar",side_effect=solar),patch("france_pv.grid",side_effect=AssertionError("grid forbidden")):
            frame,best=sample_solar(Point(0,0).buffer(10000),2025,2025,30,.8,lambda _:None,evaluator=evaluator,point_budget=16)
        self.assertLessEqual(len(evaluator.records),4)
        self.assertEqual(best["yield"],frame["yield"].max())

    def test_network_positions_reuse_samples_without_weather_requests(self):
        row=dict(x=0,y=0,**{"yield":1500})
        with patch("france_pv.solar",side_effect=AssertionError("weather request forbidden")):
            result=network_proxy_candidates(Point(0,0).buffer(1000),Point(0,0),[dict(kind="transformer",geometry=Point(500,0))],[row])
        self.assertEqual(result[0]["x"],500)
        self.assertEqual(result[0]["yield"],1500)
        self.assertEqual(result[0]["distance_meteo_proxy_m"],500)

    def test_irradiance_aplats_cover_area_without_overlap(self):
        origin=core.metric(Point(-4.4,36.7))
        area=origin.buffer(1000)
        samples=pd.DataFrame([dict(x=origin.x-500,y=origin.y,irradiance_kwh_m2=1700,**{"yield":1360}),dict(x=origin.x+500,y=origin.y,irradiance_kwh_m2=1800,**{"yield":1440})])
        features=irradiance_cells(area,samples)
        geometries=[core.metric(shape(f["geometry"])) for f in features]
        self.assertEqual(len(features),2)
        self.assertLess(area.symmetric_difference(unary_union(geometries)).area,1)
        self.assertLess(geometries[0].intersection(geometries[1]).area,1)
        self.assertEqual(features[0]["properties"]["irradiance_kwh_m2_an"],1700)

    def test_junction_is_nearest_field_edge_instead_of_center(self):
        area=box(4000000,2800000,4001000,2801000)
        parcel=dict(parcelle="P",geometry=area.representative_point(),footprint=area,pv_m2=100)
        asset=dict(id="line/1",kind="transformer",voltage="20000",geometry=Point(4001100,2800500))
        assumptions=dict(cable_low=80,cable_high=250,detour=1.3,fixed_low=1,fixed_high=2,contingency=.2)
        plan=shared_connection_plan([parcel],[asset],assumptions)
        self.assertAlmostEqual(plan["external_length_m"],100)
        self.assertTrue(area.covers(plan["hub"]))
        self.assertLess(plan["external_length_m"],parcel["geometry"].distance(asset["geometry"]))

    def test_disconnected_fields_are_split_without_losing_modules(self):
        point=core.metric(Point(-4.4,36.7))
        geometry=MultiPolygon([point.buffer(10),Point(point.x+100,point.y).buffer(20)])
        features=[dict(type="Feature",geometry=mapping(core.geographic(geometry)),properties=dict(idu="ABC",pv_m2=1000,emprise_m2=2500))]
        fields=split_fields(features)
        self.assertEqual(len(fields),2)
        self.assertAlmostEqual(sum(f["properties"]["pv_m2"] for f in fields),1000)
        self.assertEqual({f["properties"]["idu"] for f in fields},{"ABC"})
        self.assertEqual(len({f["properties"]["champ_id"] for f in fields}),2)

    def test_collector_segments_really_join_at_one_junction_per_field(self):
        areas=[box(4000000+i*200,2800000,4000100+i*200,2800100) for i in range(3)]
        parcels=[dict(parcelle=f"P{i}",geometry=a.representative_point(),footprint=a,pv_m2=100) for i,a in enumerate(areas)]
        asset=dict(id="node/1",kind="transformer",voltage="20000",geometry=Point(3999900,2800050))
        plan=shared_connection_plan(parcels,[asset],dict(cable_low=80,cable_high=250,detour=1.3,fixed_low=1,fixed_high=2,contingency=.2))
        junctions={}
        for feature in plan["features"]:
            props=feature["properties"]
            path=core.metric(shape(feature["geometry"]))
            junctions.setdefault(props["parcelle"],[]).append(Point(path.coords[0]))
            if props["segment_type"]=="collecteur":
                junctions.setdefault(props["ouvrage_id"].split("/",1)[1],[]).append(Point(path.coords[-1]))
        for key,points in junctions.items():
            self.assertTrue(all(points[0].distance(p)<.01 for p in points))
            self.assertLess(areas[int(key[1:])].distance(points[0]),.01)
        self.assertEqual(plan["connections"],1)



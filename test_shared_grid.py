import unittest
from shapely.geometry import Point
from france_pv import parse_osm
from shared_grid import shared_connection_plan, shared_capex, transformer_sizing
from electrical_data import rating_kva, apply_verified_inventory


class SharedGridTests(unittest.TestCase):
    def setUp(self):
        self.assumptions = dict(detour=1.3, cable_low=80, cable_high=250,
            collector_low=40, collector_high=120, fixed_low=20000, fixed_high=100000,
            transformer_low=30000, transformer_high=80000, contingency=.25,
            pv_low=650, pv_high=1200)
        self.parcels = [dict(parcelle=f"P{i}", geometry=Point(4000000+i*100, 2800000), pv_m2=1000) for i in range(3)]
        self.asset = dict(id="OSM/node/9", kind="transformer", voltage="20000", geometry=Point(3999900,2800000))

    def test_fields_form_one_tree_with_one_external_connection(self):
        plan = shared_connection_plan(self.parcels, [self.asset], self.assumptions)
        internal = [f for f in plan["features"] if f["properties"]["segment_type"] == "collecteur"]
        external = [f for f in plan["features"] if f["properties"]["segment_type"] == "raccordement_unique"]
        self.assertEqual((len(internal),len(external),plan["connections"]), (2,1,1))
        self.assertEqual(plan["collector_length_m"],200)
        self.assertEqual(plan["external_length_m"],100)
        self.assertEqual(plan["path_lengths_m"], {"P0":100,"P1":200,"P2":300})
        self.assertEqual(external[0]["properties"]["puissance_transitee_kwp"],630)
        cost = shared_capex(plan,630,self.assumptions,[])
        self.assertEqual(cost["connection_low"],200*1.3*40+100*1.3*80+20000)
        self.assertEqual(cost["transformer_low"],30000)
        self.assertEqual(sum(f["properties"]["interface_basse_eur"] for f in plan["features"]),20000)

    def test_no_external_asset_preserves_collection_but_no_total_cost(self):
        plan = shared_connection_plan(self.parcels,[],self.assumptions)
        self.assertEqual(len(plan["features"]),2)
        self.assertEqual(plan["connections"],0)
        self.assertIsNone(shared_capex(plan,630,self.assumptions,[])["high"])
        self.assertTrue(all(v is None for v in plan["path_lengths_m"].values()))

    def test_osm_pole_is_not_a_grid_connection_target(self):
        pole={**self.asset,"kind":"pole"}
        plan=shared_connection_plan(self.parcels,[pole],self.assumptions)
        self.assertEqual(plan["connections"],0)

    def test_project_sizing_is_labelled_and_conserves_power(self):
        result = transformer_sizing(2100)
        self.assertAlmostEqual(result["minimum_kva"], 2100/1.2/.95*1.2)
        self.assertGreaterEqual(result["project_transformer_kva"],result["minimum_kva"])
        self.assertIn("pas une valeur",result["status"])


class ElectricalValuesTests(unittest.TestCase):
    def test_osm_rating_units_are_normalized(self):
        self.assertEqual(rating_kva("70000000"),70000)
        self.assertEqual(rating_kva("63 MVA"),63000)
        self.assertEqual(rating_kva("630 kVA"),630)
        self.assertIsNone(rating_kva("inconnue"))

    def test_primary_secondary_and_rating_are_read_from_same_object(self):
        assets,_,_ = parse_osm({"elements":[dict(type="node",id=1,lat=40.3,lon=-3.8,
            tags={"power":"transformer","voltage:primary":"20000","voltage:secondary":"400","rating":"630000"})]})
        self.assertEqual(assets[0]["voltage"],"20000")
        self.assertEqual(assets[0]["secondary_voltage_v"],400)
        self.assertEqual(assets[0]["power_kva"],630)

    def test_inventory_exact_ids_provenance_and_positive_values(self):
        assets=[dict(id="A"),dict(id="B")]
        updated,matched=apply_verified_inventory(assets,"asset_id,source,power_kva\nA,plaque vérifiée,630\n")
        self.assertEqual(matched,1)
        self.assertEqual(updated[0]["power_kva"],630)
        self.assertNotIn("power_kva",updated[1])
        for text in ("asset_id,source,power_kva\nA,s,-1\n", "asset_id,source,power_kva\nA,,630\n", "asset_id,source\nA,s\nA,s\n"):
            with self.assertRaises(ValueError):
                apply_verified_inventory(assets,text)

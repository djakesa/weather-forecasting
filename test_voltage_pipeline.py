import unittest
from unittest.mock import patch
from shapely.geometry import Point
from electrical_data import voltage_label,apply_verified_inventory
from map_ui import base_map,add_network_layers
from transmission_sources import rte_reference_sites
import france_pv as core


class VoltagePipelineTests(unittest.TestCase):
    def test_secondary_only_never_produces_unknown_volts_in_tooltip(self):
        asset=dict(id="node/1",kind="transformer",voltage="inconnue",secondary_voltage_v=400,geometry=core.metric(Point(6.14,45.85)))
        self.assertEqual(voltage_label(asset),"secondaire 0.4 kV")
        m=base_map(45.85,6.14)
        add_network_layers(m,[asset])
        html=m.get_root().render()
        self.assertIn("secondaire 0.4 kV",html)
        self.assertNotIn("inconnue V",html)

    def test_missing_voltage_is_explicitly_not_published(self):
        self.assertEqual(voltage_label(dict(voltage="inconnue")),"Tension non publiée par cette source")

    def test_final_connection_reuses_the_same_secondary_voltage_label(self):
        from shared_grid import shared_connection_plan
        area=core.metric(Point(6.14,45.85))
        asset=dict(id="node/1",kind="transformer",voltage="inconnue",secondary_voltage_v=400,geometry=Point(area.x+100,area.y))
        assumptions=dict(cable_low=80,cable_high=250,detour=1.3,fixed_low=1,fixed_high=2,contingency=.2)
        plan=shared_connection_plan([dict(parcelle="P",geometry=area,pv_m2=100)],[asset],assumptions)
        self.assertEqual(plan["features"][0]["properties"]["tension_source_libelle"],"secondaire 0.4 kV")

    def test_csv_primary_secondary_reach_report_in_kv(self):
        asset=dict(id="A",kind="transformer",voltage="inconnue",geometry=core.metric(Point(6.14,45.85)))
        assets,_=apply_verified_inventory([asset],"asset_id,source,primary_voltage_v,secondary_voltage_v\nA,plaque,20000,400\n")
        self.assertEqual(voltage_label(assets[0]),"primaire 20 kV · secondaire 0.4 kV")
        report=core.asset_table(assets,asset["geometry"]).iloc[0]
        self.assertEqual(report["Tension primaire (kV)"],20)
        self.assertEqual(report["Tension secondaire (kV)"],.4)

    def test_generic_csv_voltage_is_not_invented_as_primary(self):
        assets,_=apply_verified_inventory([dict(id="A",kind="transformer")],"asset_id,source,voltage_v\nA,plaque,20000\n")
        self.assertNotIn("primary_voltage_v",assets[0])
        self.assertEqual(voltage_label(assets[0]),"20 kV")

    def test_primary_only_csv_high_voltage_is_not_distribution_target(self):
        asset=dict(id="A",kind="transformer",geometry=Point(0,0),voltage="inconnue")
        assets,_=apply_verified_inventory([asset],"asset_id,source,primary_voltage_v\nA,plaque,225000\n")
        self.assertIsNone(core.connection(Point(1,0),assets))

    def test_rte_sites_require_exact_reference_and_do_not_fill_transformer_windings(self):
        asset=dict(id="way/1",kind="substation",rte_reference="FRAIS",geometry=Point(1,1))
        data=dict(results=[dict(code_poste="OTHER",tension="63kV"),dict(code_poste="FRAIS",tension="225kV",nom_poste="FRAISES")])
        with patch("france_pv.api",return_value=data):
            result=rte_reference_sites([asset])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["voltage"],"225kV")
        self.assertNotIn("primary_voltage_v",result[0])
        self.assertFalse(result[0]["planning_eligible"])
        with patch("france_pv.api") as api:
            self.assertEqual(rte_reference_sites([dict(kind="transformer",geometry=Point(1,1))]),[])
            api.assert_not_called()



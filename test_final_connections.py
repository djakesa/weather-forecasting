import unittest
from unittest.mock import patch

from shapely.geometry import Point, LineString, mapping, shape

from france_pv import (connection, parcel_connections, geographic, metric,
                       ign_electrical_assets, voltage_in_volts)
from map_ui import base_map, add_connection_layer


class FinalConnectionTests(unittest.TestCase):
    def setUp(self):
        self.point = Point(700000, 6600000)
        self.asset = {"id": "Enedis/line", "source": "Enedis", "kind": "line", "voltage": "20000",
                      "geometry": LineString([(700100, 6599900), (700100, 6600100)])}
        self.assumptions = dict(detour=1.3, cable_low=80, cable_high=250,
                                fixed_low=20000, fixed_high=100000, transformer_low=30000,
                                transformer_high=80000, contingency=.25)

    def test_final_link_geometry_distances_and_costs_are_consistent(self):
        link = connection(self.point, [self.asset])
        result = parcel_connections([{"parcelle": "P1", "geometry": self.point, "link": link}], self.assumptions)
        self.assertEqual(len(result), 1)
        properties = result[0]["properties"]
        self.assertAlmostEqual(properties["distance_directe_m"], 100)
        self.assertAlmostEqual(properties["longueur_budget_m"], 130)
        self.assertAlmostEqual(properties["liaison_basse_eur"], 10400)
        self.assertAlmostEqual(properties["raccordement_bas_eur"], (10400 + 20000 + 30000) * 1.25)
        path = metric(shape(result[0]["geometry"]))
        self.assertLess(Point(path.coords[0]).distance(self.point), .001)
        self.assertLess(Point(path.coords[-1]).distance(link["target"]), .001)
        m = base_map(44.9, 3)
        add_connection_layer(m, result)
        html = m.get_root().render()
        self.assertIn("P1", html)
        self.assertIn("#ef4444", html)
        self.assertIn("Enedis/line", html)

    def test_missing_target_does_not_create_a_phantom_link(self):
        self.assertEqual(parcel_connections([{"parcelle": "P1", "geometry": self.point, "link": None}], self.assumptions), [])

    def test_ign_voltage_is_normalized_but_not_transferred_to_other_assets(self):
        features = [{"id": "line.1", "geometry": mapping(geographic(self.asset["geometry"])),
                     "properties": {"cleabs": "IGN-L1", "voltage": "63 kV", "etat_de_l_objet": "En service"}}]
        with patch("france_pv.wfs_features", side_effect=[features, []]):
            assets = ign_electrical_assets(self.point.buffer(1000))
        self.assertEqual(assets[0]["voltage"], "63000")
        self.assertEqual(assets[0]["voltage_raw"], "63 kV")
        self.assertFalse(assets[0]["planning_eligible"])
        self.assertIsNone(connection(self.point, assets))
        self.assertIsNone(assets[0]["power_kva"])

    def test_nominal_voltage_requires_an_explicit_number(self):
        self.assertEqual(voltage_in_volts("20 kV"), 20000)
        self.assertEqual(voltage_in_volts("0,4 kV"), 400)
        self.assertEqual(voltage_in_volts("20000"), 20000)
        self.assertIsNone(voltage_in_volts("HTA/BT"))
        self.assertIsNone(voltage_in_volts("63;225 kV"))

    def test_osm_high_voltage_line_is_not_selected_as_distribution_target(self):
        high = {**self.asset, "source": "OSM", "voltage": "225000"}
        self.assertIsNone(connection(self.point, [high]))


if __name__ == "__main__":
    unittest.main()

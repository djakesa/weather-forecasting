import unittest

from shapely.geometry import Point, LineString
from map_ui import base_map, add_network_layers, asset_popup


class MapTests(unittest.TestCase):
    def test_aerial_plan_and_gps_selection_are_available(self):
        output = base_map(44.9, 3., select_gps=True).get_root().render()
        self.assertIn("ORTHOIMAGERY.ORTHOPHOTOS", output)
        self.assertIn("openstreetmap", output)
        self.assertIn("locationfound", output)
        self.assertIn("this.fire('click'", output)
        self.assertIn("enableHighAccuracy", output)

    def test_electrical_popups_keep_values_and_escape_source_text(self):
        asset = {"id": "<script>", "kind": "transformer", "voltage": "20000", "power_kva": "630 kVA", "geometry": Point(700000, 6600000)}
        popup = asset_popup(asset)
        self.assertIn("20000", popup)
        self.assertIn("630 kVA", popup)
        self.assertNotIn("<script>", popup)
        result = base_map(44.9, 3.)
        add_network_layers(result, [asset, {**asset, "id": "line", "kind": "line", "geometry": LineString([(700000,6600000),(700100,6600100)])}])
        self.assertIn("Puissance", result.get_root().render())


if __name__ == "__main__":
    unittest.main()

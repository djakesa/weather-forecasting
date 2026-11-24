import unittest
from unittest.mock import patch

from shapely.geometry import Point, box, mapping, shape

from france_pv import allocate, capex, geographic, metric, solar


class ScreeningTests(unittest.TestCase):
    def test_discontinuous_parcels_respect_requested_surface(self):
        a, b = box(700000, 6600000, 700100, 6600100), box(700150, 6600000, 700250, 6600100)
        features = [{"type": "Feature", "geometry": mapping(geographic(g)), "properties": {"idu": str(i)}} for i, g in enumerate([a, b])]
        selected, missing = allocate(features, a.union(b), Point().buffer(0), a.union(b), a.centroid, 5000, .4)
        self.assertEqual(len(selected), 2)
        self.assertLess(missing, .01)
        self.assertAlmostEqual(sum(f["properties"]["pv_m2"] for f in selected), 5000, places=3)
        for feature in selected:
            self.assertLess(metric(shape(feature["geometry"])).difference(a.union(b)).area, .001)

    def test_insufficient_land_is_reported(self):
        a = box(700000, 6600000, 700100, 6600100)
        selected, missing = allocate([], a, a, a, a.centroid, 5000, .4)
        self.assertEqual(selected, [])
        self.assertEqual(missing, 5000)

    def test_missing_network_does_not_produce_total_price(self):
        assumptions = dict(pv_low=650, pv_high=1200, cable_low=80, cable_high=250, fixed_low=20000, fixed_high=100000, detour=1.3, contingency=.25)
        self.assertIsNone(capex(1000, None, assumptions)["low"])
        self.assertAlmostEqual(capex(1000, 100, assumptions)["low"], (650000 + 10400 + 20000) * 1.25)

    @patch("france_pv.api")
    def test_missing_weather_is_rejected(self, api):
        api.return_value = {"hourly": {"global_tilted_irradiance": [None], "time": ["2023-01-01T00:00"]}}
        with self.assertRaises(ValueError):
            solar([Point(700000, 6600000)], 2025, 2025, 30, .8)


if __name__ == "__main__":
    unittest.main()

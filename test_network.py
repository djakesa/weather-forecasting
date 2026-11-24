import json
import unittest
from unittest.mock import Mock, patch

import requests
from shapely.geometry import Point, mapping

from france_pv import (enedis_assets, geographic, land_context, metric, network,
                       official_land_context, public_json, wfs_features)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.center = metric(Point(3., 44.9))
        self.area = self.center.buffer(1000)

    def test_enedis_reads_geometry_paginates_and_filters_area(self):
        geometry = json.dumps(mapping(geographic(self.center)))
        first = {"results": [{"_id": "one", "geometry": geometry}],
                 "next": "https://opendata.enedis.fr/data-fair/api/v1/datasets/poste-electrique/lines?after=x"}
        next_page = {"results": [{"_id": "two", "geometry": geometry}]}
        with patch("france_pv.api", side_effect=[first, next_page, {"results": []}, {"results": []}]) as api:
            assets = enedis_assets(self.area)
            self.assertEqual(len(assets), 2)
            self.assertTrue(all(a["source"] == "Enedis" for a in assets))
            self.assertEqual(api.call_count, 4)
            self.assertIn('_geoshape',api.call_args_list[0].args[1]['select'])
            self.assertIsNone(assets[0]['voltage'])

    def test_official_network_does_not_require_osm(self):
        assets = [{"geometry": self.center, "source": "Enedis"}]
        with (patch("france_pv.enedis_assets", return_value=assets),
              patch("france_pv.ign_electrical_assets", return_value=[]),
              patch("france_pv.overpass") as osm):
            self.assertEqual(network(self.area, include_context=False)[0], assets)
            osm.assert_not_called()

    def test_official_terrain_has_all_required_obstacle_layers(self):
        def data(layer, area, progress):
            return [{"type": "Feature", "geometry": mapping(geographic(self.area)), "properties": {}}] if layer.startswith("RPG") else []
        with patch("france_pv.wfs_features", side_effect=data) as read:
            exclusions, fields = official_land_context(self.area)
            self.assertTrue(exclusions.is_empty)
            self.assertAlmostEqual(fields.area, self.area.area, places=2)
            self.assertEqual(read.call_count, 5)

    def test_official_terrain_works_with_overpass_down(self):
        metadata = {}
        expected = self.center.buffer(1), self.area
        with (patch("france_pv.official_land_context", return_value=expected),
              patch("france_pv.overpass", side_effect=ValueError("down")) as osm):
            self.assertEqual(land_context(self.area, metadata=metadata), expected)
            self.assertEqual(metadata["agricultural_year"], 2024)
            osm.assert_not_called()

    def test_terrain_falls_back_without_inventing_empty_obstacles(self):
        with (patch("france_pv.official_land_context", side_effect=ValueError("IGN down")),
              patch("france_pv.osm_land_context", side_effect=ValueError("OSM down"))):
            with self.assertRaisesRegex(ValueError, "OSM down"):
                land_context(self.area)

    def test_truncated_wfs_is_rejected(self):
        with patch("france_pv.api", return_value={"type": "FeatureCollection", "features": [], "numberMatched": 100}):
            with self.assertRaisesRegex(ValueError, "tronquée"):
                wfs_features("layer", self.area, lambda _: None)

    def test_public_service_timeout_is_bounded_and_closes_response(self):
        good = Mock(status_code=200)
        good.json.return_value = {"features": []}
        with (patch("france_pv.requests.get", side_effect=[requests.ReadTimeout(), good]) as get,
              patch("france_pv.wait_until")):
            self.assertEqual(public_json("https://example.test"), {"features": []})
            self.assertEqual(get.call_count, 2)
            good.close.assert_called_once()

    def test_long_retry_after_is_not_waited_or_retried(self):
        busy = Mock(status_code=429, headers={"Retry-After": "3600"})
        with patch("france_pv.requests.get", return_value=busy) as get:
            with self.assertRaisesRegex(ValueError, "3600"):
                public_json("https://example.test")
            self.assertEqual(get.call_count, 1)
            busy.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

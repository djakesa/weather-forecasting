import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from france_pv import overpass, retry_delay
from map_selection import update_selection


class ResilienceTests(unittest.TestCase):
    def test_retry_after_and_cache(self):
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        busy = Mock(status_code=429, headers={"Retry-After": "40"})
        good = Mock(status_code=200)
        good.json.return_value = {"elements": []}
        with (tempfile.TemporaryDirectory() as folder,
              patch("france_pv.CACHE", Path(folder)),
              patch("france_pv.OVERPASS_ENDPOINTS", ("https://example.test",)),
              patch("france_pv.time.time", side_effect=lambda: clock[0]),
              patch("france_pv.time.sleep", side_effect=sleep),
              patch("france_pv.requests.get", side_effect=[busy, good]) as post):
            def respond(*args, **kwargs):
                if post.call_count == 2:
                    self.assertGreaterEqual(clock[0], 1040)
                    return good
                return busy
            post.side_effect = respond
            self.assertEqual(overpass("query"), {"elements": []})
            self.assertEqual(post.call_count, 2)
            # Repeated analysis must reuse a cached response, not hit the quota.
            self.assertEqual(overpass("query"), {"elements": []})
            self.assertEqual(post.call_count, 2)

    def test_long_cooldown_is_persisted_without_automatic_wait(self):
        busy = Mock(status_code=429, headers={"Retry-After": "3600"})
        with (tempfile.TemporaryDirectory() as folder,
              patch("france_pv.CACHE", Path(folder)),
              patch("france_pv.OVERPASS_ENDPOINTS", ("https://example.test",)),
              patch("france_pv.requests.get", return_value=busy) as post,
              patch("france_pv.time.sleep") as sleep):
            with self.assertRaises(ValueError):
                overpass("query")
            with self.assertRaises(ValueError):
                overpass("other query")
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(list(Path(folder).glob("osm_*.json")), [])

    def test_retry_after_http_date(self):
        self.assertEqual(retry_delay("invalid", 15), 15)
        self.assertEqual(retry_delay("120", 15), 120)
        self.assertGreater(retry_delay("Wed, 01 Jan 2100 00:00:00 GMT", 15), 120)

    def test_circle_sets_coordinates_and_radius_then_click_moves_center(self):
        circle = {"geometry": {"type": "Point", "coordinates": [3.9, 43.6]}}
        state = {"search_map": {"last_active_drawing": circle, "last_circle_radius": 3.5}}
        update_selection(state)
        self.assertEqual((state["gps_lat"], state["gps_lon"], state["gps_radius"]), (43.6, 3.9, 3.5))
        state["search_map"]["last_clicked"] = {"lat": 44.0, "lng": 4.0}
        update_selection(state)
        self.assertEqual((state["gps_lat"], state["gps_lon"]), (44, 4))
        self.assertEqual(state["gps_radius"], 3.5)

    def test_invalid_circle_does_not_overwrite_inputs(self):
        state = {"gps_lat": 43.6, "gps_lon": 3.9, "gps_radius": 10., "search_map": {
            "last_active_drawing": {"geometry": {"type": "Point", "coordinates": [3.9, 43.6]}},
            "last_circle_radius": 0.5}}
        update_selection(state)
        self.assertEqual(state["gps_radius"], 10.)
        self.assertTrue(state["selection_error"])


if __name__ == "__main__":
    unittest.main()

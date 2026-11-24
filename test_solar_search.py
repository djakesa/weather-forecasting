import unittest
from unittest.mock import patch

import pandas as pd
from shapely.geometry import Point, box

from france_pv import SolarEvaluator, search_solar, solar, METEO_URL, METEO_MODEL


class SearchTests(unittest.TestCase):
    def fake_solar(self, points, *args):
        return pd.DataFrame([dict(x=p.x, y=p.y, latitude=45., longitude=3.,
                                  **{"yield": 1600 - p.distance(Point(740000, 6640000)) / 10000}) for p in points])

    def test_progressive_search_reaches_one_km_under_shared_budget(self):
        area = box(700000, 6600000, 800000, 6700000)
        with patch("france_pv.solar", side_effect=self.fake_solar):
            evaluator = SolarEvaluator(2025, 2025, 30, .8, lambda _: None, maximum=40)
            samples, best = search_solar(area, 2025, 2025, 30, .8, lambda _: None, evaluator=evaluator, point_budget=32)
            self.assertLessEqual(len(evaluator.records), 32)
            self.assertEqual(samples.pas_km.min(), 1)
            self.assertGreater(samples.pas_km.nunique(), 3)
            self.assertEqual(best["yield"], samples["yield"].max())
            self.assertTrue(all(area.covers(Point(r.x, r.y)) for r in samples.itertuples()))
            evaluator([Point(700000 + i * 10, 6600000) for i in range(8)])
            self.assertLessEqual(len(evaluator.records), 40)

    def test_point_is_evaluated_once_and_over_budget_never_sends_request(self):
        with patch("france_pv.solar", side_effect=self.fake_solar) as fetch:
            evaluator = SolarEvaluator(2025, 2025, 30, .8, lambda _: None, maximum=1)
            evaluator([Point(700000, 6600000)] * 2)
            evaluator([Point(700000, 6600000)])
            with self.assertRaisesRegex(ValueError, "Budget"):
                evaluator([Point(700100, 6600000)])
            self.assertEqual(fetch.call_count, 1)

    def test_only_gti_and_arome_requested_from_historical_forecast(self):
        times = pd.date_range("2025-01-01", "2026-01-01", freq="h", inclusive="left").strftime("%Y-%m-%dT%H:%M").tolist()
        response = {"hourly": {"time": times, "global_tilted_irradiance": [100] * len(times)}}
        with patch("france_pv.api", return_value=response) as fetch:
            data = solar([Point(700000, 6600000)], 2025, 2025, 30, .8)
            url, params = fetch.call_args.args
            self.assertEqual(url, METEO_URL)
            self.assertEqual(params["models"], METEO_MODEL)
            self.assertEqual(params["hourly"], "global_tilted_irradiance")
            self.assertNotIn("daily", params)
            self.assertAlmostEqual(data.iloc[0]["irradiance_kwh_m2"], 876)
            self.assertAlmostEqual(data.iloc[0]["yield"], 700.8)

    def test_unavailable_archive_year_is_rejected_before_http(self):
        with patch("france_pv.api") as fetch:
            with self.assertRaises(ValueError):
                solar([Point(700000, 6600000)], 2021, 2021, 30, .8)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

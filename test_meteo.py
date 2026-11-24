"""Weather retries and budgets with a virtual clock; no network calls."""
import tempfile
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import pandas as pd

import requests

from france_pv import api, meteo_weight

URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = 1000.
        for target, replacement in [("france_pv.CACHE", Path(self.folder.name)),
                                    ("france_pv.time.time", lambda: self.clock),
                                    ("france_pv.time.sleep", self.sleep)]:
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)

    def sleep(self, seconds):
        self.clock += seconds

    def params(self, locations=1):
        return dict(latitude=",".join(["44.9"] * locations), longitude=",".join(["3.0"] * locations),
                    start_date="2023-01-01", end_date="2023-12-31", hourly="global_tilted_irradiance", models="meteofrance_arome_france")

    def good(self):
        response = Mock(status_code=200)
        times = pd.date_range("2023-01-01", "2024-01-01", freq="h", inclusive="left").strftime("%Y-%m-%dT%H:%M").tolist()
        response.json.return_value = {"hourly": {"time": times, "global_tilted_irradiance": [100] * len(times)}}
        response.headers = {}
        response.iter_content.side_effect = lambda **_: [json.dumps(response.json.return_value).encode()]
        return response

    def test_invalid_irradiance_is_not_cached(self):
        response = self.good()
        response.json.return_value["hourly"]["global_tilted_irradiance"][10] = None
        with patch("france_pv.requests.get", return_value=response):
            with self.assertRaisesRegex(ValueError, "irradiance incomplète"):
                api(URL, self.params())
        self.assertEqual([p.name for p in Path(self.folder.name).glob("*.json")], ["open_meteo_limits.json"])

    def test_single_irradiance_weight_uses_fractional_provider_formula(self):
        self.assertAlmostEqual(meteo_weight(self.params()),365/140)
        self.assertAlmostEqual(meteo_weight(self.params(10)),365/14)
        self.assertEqual(meteo_weight({**self.params(),'end_date':'2023-01-14'}),1)

    def test_old_overestimated_hourly_counter_cannot_block_a_new_download(self):
        path=Path(self.folder.name,'open_meteo_limits.json')
        path.write_text(json.dumps(dict(events=[[self.clock-300,40] for _ in range(110)],until=self.clock+600,reason='régulation locale (3600s), pas une erreur serveur')))
        with patch('france_pv.requests.get',return_value=self.good()) as get:
            api(URL,self.params())
            self.assertEqual(get.call_count,1)
        self.assertEqual(self.clock,1000)
        state=json.loads(path.read_text())
        self.assertEqual(state['version'],2)
        self.assertEqual(sum(e[2]=='legacy' for e in state['events']),110)
        self.assertAlmostEqual(state['events'][-1][1],365/140)

    def test_old_confirmed_server_pause_is_not_removed_by_counter_migration(self):
        path=Path(self.folder.name,'open_meteo_limits.json')
        path.write_text(json.dumps(dict(events=[[self.clock-300,40]],until=self.clock+3600,reason='quota serveur HTTP 429')))
        with patch('france_pv.requests.get') as get:
            with self.assertRaisesRegex(ValueError,'quota serveur'):api(URL,self.params())
            get.assert_not_called()

    def test_corrected_hourly_budget_is_still_enforced(self):
        path=Path(self.folder.name,'open_meteo_limits.json')
        path.write_text(json.dumps(dict(version=2,events=[[self.clock-300,40,'v2'] for _ in range(110)],until=0)))
        with patch('france_pv.requests.get') as get:
            with self.assertRaisesRegex(ValueError,'régulation locale'):api(URL,self.params())
            get.assert_not_called()

    def test_short_server_retry_after_is_not_inflated_to_sixty_five_seconds(self):
        busy=Mock(status_code=429,headers={'Retry-After':'5'})
        busy.json.return_value={'reason':'Minutely quota'}
        with patch('france_pv.requests.get',side_effect=[busy,self.good()]) as get:
            api(URL,self.params())
            self.assertEqual(get.call_count,2)
        self.assertEqual(self.clock,1005)

    def test_invalid_json_is_retried(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("truncated JSON")
        response.iter_content.return_value = iter([b'{"hourly":'])
        with patch("france_pv.requests.get", side_effect=[response, self.good()]) as get:
            api(URL, self.params())
            self.assertEqual(get.call_count, 2)

    def test_bad_request_is_not_retried(self):
        response = Mock(status_code=400)
        response.json.return_value = {"reason": "Invalid model or time range"}
        with patch("france_pv.requests.get", return_value=response) as get:
            with self.assertRaisesRegex(ValueError, "HTTP 400"):
                api(URL, self.params())
            self.assertEqual(get.call_count, 1)

    def test_429_waits_before_retry_and_cache_skips_http(self):
        busy = Mock(status_code=429, headers={"Retry-After": "90"})
        busy.json.return_value = {"reason": "Minutely API request limit exceeded"}
        calls = []
        def fetch(*args, **kwargs):
            calls.append(self.clock)
            return busy if len(calls) == 1 else self.good()
        with patch("france_pv.requests.get", side_effect=fetch) as get:
            result = api(URL, self.params())
            self.assertGreaterEqual(calls[1] - calls[0], 90)
            self.assertEqual(api(URL, self.params()), result)
            self.assertEqual(get.call_count, 2)

    def test_hourly_quota_persists_after_relaunch(self):
        busy = Mock(status_code=429, headers={})
        busy.json.return_value = {"reason": "Hourly API request limit exceeded"}
        with patch("france_pv.requests.get", return_value=busy) as get:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "quota"):
                    api(URL, self.params())
            self.assertEqual(get.call_count, 1)

    def test_read_timeout_retries_with_backoff(self):
        messages = []
        with patch("france_pv.requests.get", side_effect=[requests.ReadTimeout("read timeout=120"), self.good()]) as get:
            api(URL, self.params(), progress=messages.append)
            self.assertEqual(get.call_count, 2)
            self.assertGreaterEqual(self.clock, 1015)
            self.assertTrue(any("ReadTimeout" in m for m in messages))

    def test_repeated_timeout_keeps_only_cooldown_and_can_resume(self):
        with patch("france_pv.requests.get", side_effect=requests.ReadTimeout()) as get:
            with self.assertRaisesRegex(ValueError, "après 4 tentatives"):
                api(URL, self.params())
            self.assertEqual(get.call_count, 4)
        self.assertEqual([p.name for p in Path(self.folder.name).glob("*.json")], ["open_meteo_limits.json"])
        with patch("france_pv.requests.get", return_value=self.good()) as get:
            api(URL, self.params())
            self.assertEqual(get.call_count, 1)

    def test_large_batch_splits_locations_and_dates_without_gaps(self):
        weights = []
        def fetch(params, progress):
            weights.append(meteo_weight(params))
            start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            times = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days + 1)]
            return {"hourly": {"time": times, "global_tilted_irradiance": [10] * len(times)}}
        params = {**self.params(10), "end_date": "2025-12-31"}
        with patch("france_pv.meteo_request", side_effect=fetch) as request:
            result = api(URL, params)
            self.assertEqual(len(result), 10)
            self.assertLessEqual(max(weights), 40)
            for location in result:
                self.assertEqual(len(location["hourly"]["time"]), 1096)
                self.assertEqual(len(set(location["hourly"]["time"])), 1096)
            count = request.call_count
            api(URL, params)
            self.assertEqual(request.call_count, count)

    def test_partial_download_reuses_completed_subsegments(self):
        completed = []
        def fetch(params, progress):
            if len(completed) == 1:
                raise ValueError("interrupted")
            completed.append(params)
            return {"hourly": {"time": [], "global_tilted_irradiance": []}}
        # Different coordinates avoid sharing identical child keys in this test.
        params = {**self.params(2), "latitude": "44.1,44.2"}
        with patch("france_pv.meteo_request", side_effect=fetch):
            with self.assertRaises(ValueError):
                api(URL, params)
        with patch("france_pv.meteo_request", return_value={"hourly": {"time": [], "global_tilted_irradiance": []}}) as fetch:
            self.assertEqual(len(api(URL, params)), 2)
            self.assertEqual(fetch.call_count, 1)

    def test_weighted_budget_survives_multiple_requests(self):
        calls = []
        def fetch(*args, **kwargs):
            calls.append(self.clock)
            return self.good()
        with patch("france_pv.requests.get", side_effect=fetch),patch('france_pv.METEO_LIMITS',((60,30),(3600,4000),(86400,9000))):
            for i in range(13):
                api(URL, {**self.params(), "latitude": str(44 + i / 100)})
        self.assertGreaterEqual(calls[-1] - calls[0], 60)

    def test_streaming_enabled_and_response_closed(self):
        response, messages = self.good(), []
        with patch("france_pv.requests.get", return_value=response) as get:
            api(URL, self.params(), progress=messages.append)
            self.assertTrue(get.call_args.kwargs["stream"])
            response.close.assert_called_once()
            self.assertTrue(any("Kio" in message for message in messages))

    def test_stream_interruption_closes_response_and_retries(self):
        broken = self.good()
        def chunks(**kwargs):
            yield b'{"hourly":'
            raise requests.ConnectionError("interrupted stream")
        broken.iter_content.side_effect = chunks
        with patch("france_pv.requests.get", side_effect=[broken, self.good()]):
            api(URL, self.params())
            broken.close.assert_called_once()

    def test_local_wait_is_identified_and_deadline_does_not_reset(self):
        messages = []
        Path(self.folder.name, "open_meteo_limits.json").write_text(json.dumps({"version":2,"events": [], "until": self.clock + 24, "reason": "régulation locale"}))
        with patch("france_pv.requests.get", return_value=self.good()) as get:
            api(URL, self.params(), progress=messages.append)
            self.assertEqual(get.call_count, 1)
            self.assertAlmostEqual(self.clock, 1024)
            self.assertTrue(any("régulation locale" in m for m in messages))

    def test_repeated_429_stops_after_two_requests(self):
        busy = Mock(status_code=429, headers={})
        busy.json.return_value = {"reason": "Minutely quota"}
        with patch("france_pv.requests.get", return_value=busy) as get:
            with self.assertRaisesRegex(ValueError, "quota"):
                api(URL, self.params())
            self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()

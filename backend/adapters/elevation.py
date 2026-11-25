"""Read batches of elevations from the IGN Géoplateforme."""
import threading
import time

import numpy as np
import requests

from backend.domain.geometry import TO_GEO


URL = "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json"
BATCH_SIZE = 5000
_request_lock = threading.Lock()
_next_request = 0.0


def ign_elevations(x, y):
    global _next_request
    lon, lat = TO_GEO(x, y)
    heights = []
    for start in range(0, len(x), BATCH_SIZE):
        batch = slice(start, start + BATCH_SIZE)
        payload = {
            "lon": "|".join(f"{v:.7f}" for v in lon[batch]),
            "lat": "|".join(f"{v:.7f}" for v in lat[batch]),
            "resource": "ign_rge_alti_wld",
            "delimiter": "|",
            "zonly": "false",
        }
        with _request_lock:
            time.sleep(max(0, _next_request - time.monotonic()))
            _next_request = time.monotonic() + 0.21
        response = requests.post(URL, json=payload, timeout=(5, 20))
        response.raise_for_status()
        try:
            values = [float(point["z"]) for point in response.json()["elevations"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Réponse altimétrique IGN invalide.") from exc
        if len(values) != len(lon[batch]):
            raise ValueError("Réponse altimétrique IGN incomplète.")
        heights.extend(values)
    heights = np.asarray(heights)
    heights[heights == -99999] = np.nan
    return heights

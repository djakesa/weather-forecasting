"""Translate map interactions to project inputs without triggering analysis."""
import json
import math


def update_selection(state, event_key="search_map"):
    event = state.get(event_key, {})
    drawing = event.get("last_active_drawing")
    signature = json.dumps([drawing, event.get("last_circle_radius")], sort_keys=True)
    click = event.get("last_clicked")
    point, radius = None, None
    if drawing and signature != state.get("last_drawing_event"):
        state["last_drawing_event"] = signature
        geometry = drawing.get("geometry", {})
        if geometry.get("type") == "Point" and event.get("last_circle_radius"):
            lon, lat = geometry["coordinates"]
            point = {"lat": lat, "lng": lon}
            # Leaflet feature radius is in metres; streamlit-folium exposes
            # last_circle_radius in kilometres.
            metres = drawing.get("properties", {}).get("radius")
            radius = metres / 1000 if metres is not None else event["last_circle_radius"]
    if point is None and click and click != state.get("last_click_event"):
        point = click
    state["last_click_event"] = click
    if point:
        lat, lon = point["lat"], point["lng"]
        country='France'
        south,north,west,east=41.,51.5,-5.5,10.
        if not (math.isfinite(lat) and math.isfinite(lon) and south <= lat <= north and west <= lon <= east):
            state["selection_error"] = f"Choisissez un point en {country}."
            return
        if radius is not None and not 1 <= radius <= 1000:
            state["selection_error"] = "Dessinez un cercle de rayon compris entre 1 et 1000 km."
            return
        state["gps_lat"], state["gps_lon"] = float(lat), float(lon)
        if radius is not None:
            state["gps_radius"] = float(round(radius, 2))
        state["selection_error"] = None

"""Cost estimates and electrical target selection."""
import re

from shapely.ops import nearest_points


def voltage_in_volts(raw):
    """Normalize one published voltage, never infer a nominal from HTA/BT."""
    if raw is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(kV|V)?\s*", str(raw), flags=re.I)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * 1000 if (match.group(2) or "").lower() == "kv" else value


def connection(point, assets):
    if not assets:
        return None
    options = []
    for asset in assets:
        if asset.get("kind") not in {"transformer", "substation", "line", "minor_line", "cable"}:
            continue
        if asset.get("planning_eligible") is False:
            continue
        if asset.get('voltage_selection_compatible') is False:
            continue
        published = [voltage_in_volts(v) for v in str(asset.get("voltage", "")).split(";")]
        known = [v for v in published if v is not None]
        if asset.get("kind") in {"transformer", "substation"} and asset.get("primary_voltage_v"):
            known.append(asset["primary_voltage_v"])
        if asset.get("kind") in {"transformer", "substation"} and asset.get("secondary_voltage_v"):
            known.append(asset["secondary_voltage_v"])
        if known and min(known) > 50000 and not asset.get('voltage_selection_matched_v'):
            continue
        target = nearest_points(point, asset["geometry"])[1]
        options.append({**asset, "distance_m": point.distance(target), "target": target})
    return min(options, key=lambda a: a["distance_m"]) if options else None


def capex(kwp, distance_m, assumptions, parcels=1, land_area_m2=0, land_price=None):
    result = {}
    for side in ("low", "high"):
        equipment = kwp * assumptions[f"pv_{side}"]
        connection_cost = None if distance_m is None else distance_m * assumptions[f"cable_{side}"] * assumptions["detour"] + assumptions[f"fixed_{side}"] * parcels
        transformer_cost = assumptions.get(f"transformer_{side}", 0) * parcels
        land_cost = land_area_m2 * land_price[side] if land_price is not None else 0
        result[side] = None if connection_cost is None else (equipment + connection_cost + transformer_cost) * (1 + assumptions["contingency"]) + land_cost
        result[f"pv_{side}"] = equipment
        result[f"connection_{side}"] = connection_cost
        result[f"transformer_{side}"] = transformer_cost
        result[f"land_{side}"] = land_cost
    return result


LOCAL_PRICE_MODE = "Ventes locales comparables"


def price_envelope(reference, uncertainty):
    if not 0 <= uncertainty <= 1 or reference < 0:
        raise ValueError("Prix et incertitude foncière invalides.")
    return {"low": reference * (1 - uncertainty), "high": reference * (1 + uncertainty)}

"""Apply an inventory by exact asset identifier."""
import math
from io import StringIO

import pandas as pd

from .costs import voltage_in_volts


def apply_verified_inventory(assets, csv_text):
    """Join exact source IDs only; every provided value needs a provenance."""
    frame = pd.read_csv(StringIO(csv_text), sep=None, engine="python", dtype={"asset_id": str})
    if not {"asset_id", "source"}.issubset(frame.columns):
        raise ValueError("CSV : asset_id et source obligatoires ; power_kva, voltage_v, primary_voltage_v, secondary_voltage_v, current_a facultatives.")
    if frame.asset_id.duplicated().any():
        raise ValueError("CSV : identifiants dupliqués.")
    lookup = frame.set_index("asset_id").to_dict("index")
    updated, matched = [], 0
    for asset in assets:
        row = lookup.get(asset["id"])
        item = dict(asset)
        if row is not None:
            if pd.isna(row["source"]) or not str(row["source"]).strip():
                raise ValueError("La provenance source doit être renseignée pour chaque valeur importée.")
            for column in ("power_kva", "voltage_v", "primary_voltage_v", "secondary_voltage_v", "current_a"):
                raw = row.get(column)
                if raw is None or pd.isna(raw):
                    continue
                value = float(raw)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f"Valeur électrique invalide : {column}.")
                item["voltage" if column == "voltage_v" else column] = str(value) if column == "voltage_v" else value
            item["electrical_value_source"] = str(row["source"])
            item["electrical_value_status"] = "Inventaire fourni par l'utilisateur, provenance déclarée"
            if item.get("kind") in {"transformer", "substation"}:
                voltages = [voltage_in_volts(item.get("voltage")), item.get("primary_voltage_v"),item.get("secondary_voltage_v")]
                if any(v is not None and 0 < v <= 50000 for v in voltages):
                    item["planning_eligible"] = True
                if item.get("available_generation_mw") == 0 or item.get("generation_reserved"):
                    item["planning_eligible"] = False
            matched += 1
        updated.append(item)
    return updated, matched

"""Build the standalone network snapshot from French network sources."""
from datetime import datetime, timezone

from connection_preferences import apply_voltage_preferences
from countries import electrical_network, search_area
from enedis_constraints import annotate, context, public

from backend.serialization import asset_features, clean, feature


def network_snapshot(config, progress, identifier):
    progress("Lecture du réseau Open Infrastructure Map…")
    area = search_area(config.lat, config.lon, config.radius, config.country)
    assets, _, _ = electrical_network(
        area, config.country, progress=progress,
        accepted_voltages_kv=config.connection_voltages_kv,
    )
    assets = apply_voltage_preferences(assets, config.connection_voltages_kv)
    zones = context(area, progress)
    assets = annotate(assets, zones)
    progress("Étape 3/3 · Préparation de la carte réseau")
    return clean(dict(
        id=identifier, kind="network", name="Réseau autour du point",
        created_at=datetime.now(timezone.utc).isoformat(),
        config=config.model_dump(), zone=feature(area, {}),
        network=asset_features(assets), grid_constraints=public(zones),
        warnings=[], summary=dict(network_count=len(assets)),
    ))

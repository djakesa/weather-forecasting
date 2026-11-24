"""Orchestrate solar analysis using injected data sources."""
import pandas as pd
from shapely.geometry import Point, shape

from backend.models import ProjectInput
from backend.application.ports import AnalysisSources, SourceUnavailable
from backend.domain.geometry import METRIC_CRS, metric, point_identity
from backend.domain.costs import LOCAL_PRICE_MODE, capex, connection, price_envelope
from spatial_sampling import scattered_points, network_proxy_candidates
from field_routing import split_fields
from shared_grid import shared_connection_plan, shared_capex
from backend.domain.inventory import apply_verified_inventory
from connection_preferences import apply_voltage_preferences, capex_for_connection


def analyze(config: ProjectInput, progress, sources: AnalysisSources):
    lat=config.lat
    lon=config.lon
    radius=config.radius
    surface=config.surface
    start=config.start
    end=config.end
    point_budget=config.point_budget
    tilt=config.tilt
    pr=config.pr
    density=config.density
    coverage=config.coverage
    loss=config.loss
    move=config.move
    price_mode=config.price_mode
    manual_price=config.manual_price
    price_uncertainty=config.price_uncertainty
    land_basis=config.land_basis
    automatic_ratings=config.automatic_ratings
    country=config.country
    country_config=sources.countries[country]
    country_config={**country_config,"model":sources.solar_model(country_config["model"], start)}
    boundary=sources.area(lat,lon,radius,country)
    if boundary is None or boundary.is_empty:
        raise ValueError("Zone vide : choisissez un point situé sur le territoire.")
    terrain_country=sources.resolve_country(lat,lon,country)
    assumptions=config.assumptions()
    progress("Étape 1/3 · Calcul du potentiel solaire")
    progress("Recherche solaire en cours…")
    evaluator = sources.solar_evaluator(start, end, tilt, pr, progress, maximum=point_budget, model=country_config["model"],persistent=True,reuse_distance_m=config.weather_reuse_m)
    samples, best = sources.sample_solar(boundary, start, end, tilt, pr, progress,
                                 evaluator=evaluator, point_budget=point_budget)
    for row in samples.to_dict("records"):
        evaluator.records[point_identity(Point(row["x"], row["y"]))] = row
    best_point = Point(best["x"], best["y"])
    progress("Étape 2/3 · Parcelles et réseau, du meilleur potentiel solaire aux secteurs suivants")
    local = boundary.intersection(best_point.buffer(move * 1000))
    progress("Lecture du réseau et comparaison locale…")
    warnings = []
    land_reference, land_price = None, None
    if price_mode == "Hypothèse manuelle":
        land_reference = dict(eur_m2=manual_price, source="Hypothèse utilisateur", year=None, department="non applicable", url=None)
    elif price_mode=="Référence nationale":
        try:
            land_reference = sources.land_reference(best_point, terrain_country)
        except Exception as exc:
            warnings.append(f"Référence de prix agricole indisponible : {exc}. Le foncier reste à chiffrer ; vous pouvez saisir une hypothèse manuelle.")
    if land_reference is not None:
        land_price = price_envelope(land_reference["eur_m2"], price_uncertainty)
    network_ok = True
    try:
        assets, _, _ = sources.network(local.buffer(1000), country, progress=progress,accepted_voltages_kv=config.connection_voltages_kv)
        if automatic_ratings:
            assets = sources.enrich_transformers(assets, local.buffer(1000), progress)
        if config.inventory_csv:
            try:
                assets, matched = apply_verified_inventory(assets, config.inventory_csv.removeprefix("﻿"))
                warnings.append(f"{matched} ouvrages avec caractéristiques de l'inventaire fourni (provenance déclarée).")
            except (ValueError, UnicodeError) as exc:
                warnings.append(f"Inventaire refusé : {exc}. Données réseau récupérées conservées.")
    except (ValueError, SourceUnavailable) as exc:
        network_ok = False
        assets, _, _ = [], Point().buffer(0), Point().buffer(0)
        warnings.append(f"Réseau indisponible : {exc}. Résultat solaire conservé ; raccordement non chiffrable. Le cadastre est interrogé séparément.")
    assets=apply_voltage_preferences(assets,config.connection_voltages_kv)
    nearby = samples[[local.covers(Point(r.x, r.y)) for r in samples.itertuples()]].copy()
    if network_ok and assets and evaluator.remaining:
        candidates = [p for p in scattered_points(local, min(2,evaluator.remaining), 2500)
                      if all(p.distance(Point(r["x"],r["y"])) >= 2500 for r in evaluator.records.values())]
        if candidates:
            try:
                nearby = pd.concat([nearby, evaluator(candidates)], ignore_index=True)
            except (ValueError, SourceUnavailable) as exc:
                warnings.append(f"Complément météo local indisponible : {exc}. Comparaison limitée aux points déjà évalués.")
    eligible = nearby[nearby["yield"] >= best["yield"] * (1 - loss / 100)].to_dict("records") + [best]
    proxies = network_proxy_candidates(local,best_point,assets,list(evaluator.records.values()))
    eligible += [r for r in proxies if r["yield"] >= best["yield"] * (1-loss/100)]
    kwp = surface * density / 1000
    for row in eligible:
        row["link"] = connection(Point(row["x"], row["y"]), assets)
        row["capex"] = capex(kwp, row["link"]["distance_m"] if row["link"] else None, assumptions,
                              land_area_m2=surface / coverage, land_price=land_price)
        row['capex']=capex_for_connection(row['capex'],row['link'])
    connected = [r for r in eligible if r["link"] is not None]
    selected = min(connected, key=lambda r:(r['capex']['high'] if r['capex']['high'] is not None else float('inf'),r['link']['distance_m'])) if connected else eligible[-1]
    site=sources.evaluate_sites(config,boundary,best_point,samples,assets,progress,sources.parcels,sources.terrain,sources.network,sources.resolve_country)
    anchor=site['center']
    parcel_area=site['area']
    terrain_country=site['country']
    assets=site['assets']
    # Sector screening may have rejected the original distribution target.
    selected['link']=connection(Point(selected['x'],selected['y']),assets)
    selected['capex']=capex_for_connection(capex(kwp,selected['link']['distance_m'] if selected['link'] else None,assumptions,
                                               land_area_m2=surface/coverage,land_price=land_price),selected['link'])
    network_ok=network_ok or bool(assets)
    features=site['features']
    land_metadata=site['metadata']
    footprints=site['footprints']
    missing=site['missing']
    parcel_computed=site['computed']
    warnings.extend(site['warnings'])
    regulatory=site['regulatory']
    parcel_rows = []
    footprints = split_fields(footprints)
    for f in footprints:
        p = metric(shape(f["geometry"])).representative_point()
        link = connection(p, assets)
        parcel_rows.append({"parcelle": f["properties"]["champ_id"],
                            "reference_cadastrale": str(f["properties"].get("idu") or f["properties"].get("id") or f["properties"]["champ_id"]),
                            "pv_m2": f["properties"]["pv_m2"], "emprise_m2": f["properties"]["emprise_m2"],
                            "surface_parcelle_m2": f["properties"]["surface_parcelle_m2"],
                            "surface_agricole_zone_m2": f["properties"]["surface_agricole_zone_m2"],
                            "part_agricole_pct": f["properties"]["part_agricole_pct"],
                            "gpu_intersections": f["properties"].get("gpu_intersections", []),
                            "code_departement": f["properties"].get("code_dep") or str(f["properties"].get("idu", ""))[:2],
                            "commune_code": f["properties"].get("code_commune") or f["properties"].get("code_insee"),
                            "land_category": f["properties"].get("code_nature_culture"),
                            "distance_m": link["distance_m"] if link else None,
                            "geometry": p, "footprint": metric(shape(f["geometry"])), "link": link})
    collector = shared_connection_plan(parcel_rows, site.get('connection_assets',assets), assumptions, density=density)
    connection_features = collector["features"]
    for row in parcel_rows:
        link = collector["target"]
        row["link"] = link
        row["distance_m"] = collector.get("path_lengths_m", {}).get(row["parcelle"])
        row["ouvrage_raccordement"] = link["id"] if link else None
        row["type_ouvrage"] = link["kind"] if link else None
        row["source_reseau"] = link.get("source", "OSM") if link else None
        row["tension_publiee_v"] = link.get("voltage") if link else None
        row["puissance_assignee_source"] = link.get("power_kva") if link else None
        row["longueur_budget_m"] = row["distance_m"] * assumptions["detour"] if row["distance_m"] is not None else None
        row["point_collecte_commun"] = collector["hub_parcel"]
    # Reuse the nearest evaluated irradiance; do not multiply calls by parcels.
    evaluated = list(evaluator.records.values())
    parcel_energy = 0
    for row in parcel_rows:
        nearest = min(evaluated, key=lambda r: row["geometry"].distance(Point(r["x"], r["y"])))
        row["distance_meteo_proxy_m"] = row["geometry"].distance(Point(nearest["x"], nearest["y"]))
        parcel_energy += row["pv_m2"] * density / 1000 * nearest["yield"]
    local_prices = {}
    if price_mode == LOCAL_PRICE_MODE and parcel_rows:
        comparison_area = boundary.intersection(site['center'].buffer(10_000))
        local_prices = sources.local_land_prices(parcel_rows, comparison_area, progress)
        references = {row["reference_cadastrale"] for row in parcel_rows}
        missing_prices = len(references - local_prices.keys())
        if missing_prices:
            warnings.append(f"Foncier DVF à chiffrer pour {missing_prices} parcelle(s) : type inconnu, données indisponibles ou ventes comparables insuffisantes dans la zone.")
    purchased_parcels = set()
    for row in parcel_rows:
        row["surface_achat_m2"] = row["surface_parcelle_m2"] if land_basis == "Parcelles entières" else row["emprise_m2"]
        if land_basis == "Parcelles entières":
            if row["reference_cadastrale"] in purchased_parcels:
                row["surface_achat_m2"] = 0
            purchased_parcels.add(row["reference_cadastrale"])
        local_reference = local_prices.get(row["reference_cadastrale"]) if price_mode == LOCAL_PRICE_MODE else land_reference
        department = row["code_departement"]
        if terrain_country == "France" and price_mode == "Référence nationale" and department and department != (land_reference or {}).get("department_code"):
            try:
                local_reference = sources.land_reference(None, terrain_country, department_code=department)
            except Exception as exc:
                local_reference = None
                warnings.append(f"Prix du département {department} indisponible : {exc}. Foncier de la parcelle à chiffrer.")
        row["prix_agricole_reference_eur_m2"] = local_reference["eur_m2"] if local_reference else None
        row["prix_source_departement"] = local_reference.get("department") if local_reference else None
        row["reference_prix_foncier"] = local_reference
        row["land_price"] = price_envelope(local_reference["eur_m2"], price_uncertainty) if local_reference else None
        row["achat_bas_eur"] = row["surface_achat_m2"] * row["land_price"]["low"] if row["land_price"] else None
        row["achat_haut_eur"] = row["surface_achat_m2"] * row["land_price"]["high"] if row["land_price"] else None
        row["achat_estime_eur"] = row["surface_achat_m2"] * local_reference["eur_m2"] if local_reference else None
    parcel_cost = shared_capex(collector, sum(r["pv_m2"] * density / 1000 for r in parcel_rows), assumptions,
                              [{"low": r["achat_bas_eur"], "high": r["achat_haut_eur"]} if r["achat_bas_eur"] is not None else None for r in parcel_rows])
    parcel_cost=capex_for_connection(parcel_cost,collector.get('target'))
    solar_link = connection(best_point, assets)
    scenarios = [dict(name="1 · Potentiel solaire", point=best_point, energy=best["yield"] * kwp,
                      link=solar_link, cost=capex(kwp, solar_link["distance_m"] if solar_link else None, assumptions,
                                                land_area_m2=surface / coverage, land_price=land_price)),
                 dict(name="2 · Compromis réseau", point=Point(selected['x'],selected['y']), energy=selected["yield"] * kwp, link=selected["link"], cost=selected["capex"],distance_meteo_proxy_m=selected.get("distance_meteo_proxy_m",0)),
                 dict(name="3 · Parcelles", point=collector.get("hub", anchor), energy=parcel_energy, link=None, cost=parcel_cost)]
    scenarios[1]["unavailable"] = selected["link"] is None
    scenarios[2]["unavailable"] = not parcel_computed or not footprints
    scenarios[0]['cost']=capex_for_connection(scenarios[0]['cost'],solar_link)
    if any(s['cost'].get('grid_quote_required') for s in scenarios):warnings.append('Raccordement HTB sélectionné : étude et devis spécifiques nécessaires ; le CAPEX total n’est pas chiffré avec les hypothèses de distribution.')
    for scenario in scenarios[:2]:
        scenario["land_included"] = land_price is not None
    scenarios[2]["land_included"] = bool(parcel_rows) and all(r["land_price"] is not None for r in parcel_rows)
    if network_ok and not assets:
        warnings.append("Aucun ouvrage électrique publié dans les secteurs interrogés. La couverture des sources peut être incomplète ; cela ne prouve pas l’absence de réseau. Raccordement à confirmer avec le gestionnaire.")
    elif assets and selected["link"] is None:
        warnings.append("Aucun point de raccordement correspondant aux tensions cochées n’a été identifié. Les autres ouvrages restent visibles ; choisissez une autre tension publiée, une autre zone ou complétez un inventaire vérifié. Le raccordement reste à confirmer par le gestionnaire.")
    result = dict(area=boundary, samples=samples, assets=assets, footprints=footprints,
        missing=missing, scenarios=scenarios, parcels=parcel_rows, warnings=warnings, surface=surface, kwp=kwp,
        assumptions=assumptions, period=[start, end], coverage=coverage,
        weather_points=len(evaluator.records), weather_budget=point_budget, weather_model=country_config["model"],
        weather_cache_hits=evaluator.cache_hits,weather_db_hits=evaluator.db_hits,weather_nearby_hits=evaluator.nearby_hits,weather_api_points=evaluator.api_points,monthly_irradiance=best.get("monthly_irradiance_kwh_m2"),
        cadastral_features=features, parcel_computed=parcel_computed, land_metadata=land_metadata,
        land_reference=land_reference, land_price=land_price, land_basis=land_basis,
        connection_features=connection_features, collector=collector, country=country,
        terrain_country=terrain_country,regulatory=regulatory,site_comparison=site['reports'],grid_constraints=site.get('grid_constraints',{}),
        schema_version=9, metric_crs=METRIC_CRS, performance_ratio=pr,
        selection=(country,round(lat,6),round(lon,6),radius))
    return result

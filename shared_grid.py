"""Single grid connection, straight-line collector tree and explicit sizing."""
from __future__ import annotations

import math
from shapely.geometry import LineString, mapping

from backend.domain.costs import connection
from backend.domain.geometry import geographic
from field_routing import optimized_junctions
from electrical_data import voltage_label


def transformer_sizing(kwp, dc_ac_ratio=1.2, power_factor=.95, margin=.2, step_kva=100):
    if kwp < 0 or dc_ac_ratio <= 0 or not 0 < power_factor <= 1 or margin < 0 or step_kva <= 0:
        raise ValueError("Hypothèses de dimensionnement électrique invalides.")
    ac_kw = kwp / dc_ac_ratio
    required = ac_kw / power_factor * (1 + margin)
    return {"ac_kw": ac_kw, "minimum_kva": required,
            "project_transformer_kva": math.ceil(required / step_kva) * step_kva,
            "dc_ac_ratio": dc_ac_ratio, "power_factor": power_factor, "margin": margin,
            "status": "Dimensionnement calculé du projet, pas une valeur du transformateur existant"}


def shared_connection_plan(parcels, assets, assumptions, density=210):
    """MST between fields, one root in a field and one external connection.

    This is a metric straight-line heuristic, not a routed or certified cable
    design. The external node can never be a branching point of the collector.
    """
    if not parcels:
        return dict(features=[], target=None, collector_length_m=0., external_length_m=None,
                    total_length_m=None, hub_parcel=None, connections=0, sizing=transformer_sizing(0))
    points, edges, root, target = optimized_junctions(parcels, assets)
    weights = [p["pv_m2"] * density / 1000 for p in parcels]
    subtree = list(weights)
    for child, parent, _ in reversed(edges):
        subtree[parent] += subtree[child]
    collector_voltage = assumptions.get("collector_voltage_kv", 20.)
    ratio, pf = assumptions.get("dc_ac_ratio", 1.2), assumptions.get("power_factor", .95)
    features = []
    def add(start, end, length, parcel_id, destination, segment_type, power_kwp):
        external = segment_type == "raccordement_unique"
        low = assumptions["cable_low"] if external else assumptions.get("collector_low", assumptions["cable_low"])
        high = assumptions["cable_high"] if external else assumptions.get("collector_high", assumptions["cable_high"])
        budget = length * assumptions["detour"]
        props = dict(parcelle=parcel_id, ouvrage_id=destination, ouvrage_type=target["kind"] if external else "collecteur_interne",
                     source=target.get("source", "OSM") if external else "Modèle collecteur du projet",
                     tension_v_source=target.get("voltage", "inconnue") if external else "hypothèse projet",
                     tension_source_libelle=voltage_label(target) if external else "Hypothèse du collecteur du projet",
                     puissance_assignee_source=target.get("power_kva") if external else None,
                     capacite_generation_disponible_mw_source=target.get("available_generation_mw") if external else None,
                     publication_reseau=target.get("publication_date") if external else None,
                     lien_publication_reseau=target.get("source_url") if external else None,
                     segment_type=segment_type, distance_directe_m=length, longueur_budget_m=budget,
                     liaison_basse_eur=budget * low, liaison_haute_eur=budget * high,
                     interface_basse_eur=assumptions["fixed_low"] if external else 0,
                     interface_haute_eur=assumptions["fixed_high"] if external else 0,
                     transformateur_neuf_bas_eur=assumptions.get("transformer_low", 0) if external else 0,
                     transformateur_neuf_haut_eur=assumptions.get("transformer_high", 0) if external else 0,
                     puissance_transitee_kwp=power_kwp,
                     tension_collecteur_hypothese_kv=collector_voltage,
                     intensite_collecteur_calculee_a=power_kwp / ratio / pf / (math.sqrt(3) * collector_voltage),
                     statut="Collecteur indicatif, un seul raccordement externe ; tracés et capacité à valider")
        props["raccordement_bas_eur"] = (props["liaison_basse_eur"] + props["interface_basse_eur"] + props["transformateur_neuf_bas_eur"]) * (1 + assumptions["contingency"])
        props["raccordement_haut_eur"] = (props["liaison_haute_eur"] + props["interface_haute_eur"] + props["transformateur_neuf_haut_eur"]) * (1 + assumptions["contingency"])
        if external and target.get('voltage_selection_matched_v') and min(target['voltage_selection_matched_v'])>50000:
            for key in ('liaison_basse_eur','liaison_haute_eur','interface_basse_eur','interface_haute_eur','transformateur_neuf_bas_eur','transformateur_neuf_haut_eur','raccordement_bas_eur','raccordement_haut_eur'):props[key]=None
            props['grid_quote_required']=True
            props['statut']='Raccordement HTB indicatif : étude et devis spécifiques, coûts non chiffrés'
        if external:props['tensions_selectionnees_v']=target.get('voltage_selection_matched_v',[])
        features.append({"type": "Feature", "geometry": mapping(geographic(LineString([start, end]))), "properties": props})
    for child, parent, length in edges:
        add(points[child], points[parent], length, parcels[child]["parcelle"],
            "Collecte/" + parcels[parent]["parcelle"], "collecteur", subtree[child])
    if target:
        add(points[root], target["target"], target["distance_m"], parcels[root]["parcelle"],
            target["id"], "raccordement_unique", sum(weights))
    collector_length = sum(e[2] for e in edges)
    external_length = target["distance_m"] if target else None
    distances_from_hub = {root: 0.}
    for child, parent, length in edges:
        distances_from_hub[child] = distances_from_hub[parent] + length
    return dict(features=features, target=target, collector_length_m=collector_length,
                external_length_m=external_length,
                total_length_m=collector_length + external_length if target else None,
                hub_parcel=parcels[root]["parcelle"], hub=points[root], connections=1 if target else 0,
                path_lengths_m={parcels[i]["parcelle"]: distance + external_length if external_length is not None else None for i, distance in distances_from_hub.items()},
                sizing=transformer_sizing(sum(weights), ratio, pf, assumptions.get("transformer_margin", .2)))


def shared_capex(plan, kwp, assumptions, land_costs):
    result = {}
    quote_required=any(f['properties'].get('grid_quote_required') for f in plan['features'])
    for side in ("low", "high"):
        cable_key = "liaison_basse_eur" if side == "low" else "liaison_haute_eur"
        pv = kwp * assumptions[f"pv_{side}"]
        values=[f['properties'][cable_key] for f in plan['features']]
        cable = sum(values) if all(v is not None for v in values) else None
        interface = assumptions[f"fixed_{side}"] if plan["connections"] else 0
        transformer = None if quote_required else assumptions.get(f"transformer_{side}", 0) if plan["connections"] else 0
        land = sum(cost[side] for cost in land_costs if cost is not None)
        connection_cost = cable + interface if plan["connections"] and cable is not None else None
        result.update({f"pv_{side}": pv, f"connection_{side}": connection_cost,
                       f"transformer_{side}": transformer, f"land_{side}": land,
                       f"collector_and_cable_{side}": cable})
        result[side] = (pv + connection_cost + transformer) * (1 + assumptions["contingency"]) + land if connection_cost is not None and transformer is not None else None
    if quote_required:result['grid_quote_required']=True
    return result

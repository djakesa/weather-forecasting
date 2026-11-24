"""Transport-network voltage supplements, kept separate from winding ratings."""
import france_pv as core

RTE="https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/postes-electriques-rte/records"


def rte_reference_sites(assets,progress=lambda _:None):
    references={}
    for asset in assets:
        reference=asset.get("rte_reference") or asset.get("osm_tags",{}).get("ref:FR:RTE")
        if reference and asset.get("kind")=="substation":
            references.setdefault(str(reference),asset)
    result=[]
    if len(references)>20:
        raise ValueError("Complément RTE limité à 20 références par analyse ; réduire la zone.")
    for reference,asset in references.items():
        # ODS exact equality, JSON quoting prevents modifying the filter.
        import json
        progress(f"RTE : tension du site identifié par référence exacte {reference}…")
        data=core.api(RTE,{"where":"code_poste = "+json.dumps(reference),"limit":100},progress=progress)
        for record in data.get("results",[]):
            if record.get("code_poste")!=reference:
                continue
            value=record.get("tension")
            if not value:
                continue
            result.append(dict(id=f"RTE/{reference}/{value}",kind="substation",geometry=asset["geometry"],
                source="RTE / ODRÉ — tension de site",name=record.get("nom_poste"),voltage=value,
                voltage_raw=value,power_kva=None,current_a=None,planning_eligible=False,
                planning_reason="site de transport, pas un transformateur de distribution caractérisé",
                electrical_value_source=RTE,source_url=RTE,rte_reference=reference,
                coordinate_precision=f"Géométrie {asset['id']} liée par ref:FR:RTE exacte ; pas par proximité",
                network_comments="La tension publiée décrit le site ; elle ne renseigne pas les enroulements d'un transformateur individuel."))
    return result



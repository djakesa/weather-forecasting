"""Match requested grid voltages against published values, never inferred classes."""
import math

VOLTAGES_KV=(.4,5.5,6,10,15,20,30,33,45,63,90,150,225,400)
DEFAULT_VOLTAGES_KV=(15,20,30)


def published_voltages(asset):
    from france_pv import osm_voltage_values,voltage_in_volts
    values=list(osm_voltage_values(asset.get('voltage')))
    values.extend(asset.get('voltage_values_v') or [])
    tags=asset.get('osm_tags') or {}
    if asset.get('kind') in {'transformer','substation'}:
        for key,tag in [('primary_voltage_v','voltage:primary'),('secondary_voltage_v','voltage:secondary'),('tertiary_voltage_v','voltage:tertiary')]:
            value=asset.get(key)
            if value is None:value=voltage_in_volts(tags.get(tag))
            if value is not None:values.append(value)
    result=[]
    for value in values:
        try:number=float(value)
        except (TypeError,ValueError):continue
        if math.isfinite(number) and number>0 and number not in result:result.append(number)
    return result


def apply_voltage_preferences(assets,accepted_kv):
    accepted=[float(v)*1000 for v in accepted_kv]
    result=[]
    for source in assets:
        asset=dict(source)
        values=published_voltages(asset)
        matched=[v for v in values if any(abs(v-target)<=max(1,target*.005) for target in accepted)]
        original=asset.get('planning_eligible_without_voltage_filter',asset.get('planning_eligible',True))
        asset['planning_eligible_without_voltage_filter']=original
        asset['planning_eligible']=original
        # IGN originally excluded HTB only because it defaulted to distribution.
        if matched and asset.get('source')=='IGN BD TOPO' and asset.get('voltage_class')=='HTB':asset['planning_eligible']=True
        if asset.get('available_generation_mw')==0 or asset.get('generation_reserved'):asset['planning_eligible']=False
        if 'SATUREE' in asset.get('enedis_zone_statuses',[]):
            matched=[v for v in matched if v>50000]
            if not matched:asset['planning_eligible']=False
        asset['voltage_selection_compatible']=bool(matched)
        asset['voltage_selection_matched_v']=matched
        asset['voltage_selection_reason']='Tension sélectionnée publiée' if matched else 'Tension non publiée' if not values else 'Tension hors sélection'
        result.append(asset)
    return result


def capex_for_connection(cost,link):
    """HTB routing can be screened, but distribution quotes cannot price it."""
    if not link:return cost
    matched=link.get('voltage_selection_matched_v') or []
    if matched and min(matched)>50000:
        cost={**cost,'low':None,'high':None,'connection_low':None,'connection_high':None,'grid_quote_required':True}
        for key in ('transformer_low','transformer_high','collector_and_cable_low','collector_and_cable_high'):
            if key in cost:cost[key]=None
    return cost

"""Unlevered PV cash flows in constant euros, independent from data providers."""
import math
from pydantic import BaseModel,ConfigDict,Field


class EconomicsInput(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    scenario_index:int=Field(default=2,ge=0,le=2)
    years:int=Field(default=25,ge=1,le=60)
    sale_price_eur_mwh:float=Field(default=80,ge=0,le=2000)
    opex_eur_kwp_year:float=Field(default=18,ge=0,le=1000)
    additional_opex_eur_year:float=Field(default=0,ge=0,le=100_000_000)
    discount_percent:float=Field(default=5,ge=0,le=30)
    degradation_percent:float=Field(default=.5,ge=0,le=10)
    replacement_year:int=Field(default=15,ge=1,le=60)
    replacement_eur_kwp:float=Field(default=60,ge=0,le=5000)
    decommissioning_eur:float=Field(default=0,ge=0,le=1_000_000_000)
    capex_override_eur:float|None=Field(default=None,ge=0,le=10_000_000_000)


def payback(cumulative):
    if cumulative[0]>=0:return 0.
    for year in range(1,len(cumulative)):
        if cumulative[year]>=0 and cumulative[year-1]<0:
            return year-1-cumulative[year-1]/(cumulative[year]-cumulative[year-1])
    return None


def simulate(capex,energy_kwh,kwp,params,price=None):
    price=params.sale_price_eur_mwh if price is None else price
    rate=params.discount_percent/100;degradation=params.degradation_percent/100
    cumulative=discounted=-capex;cost_pv=capex;energy_pv=0
    rows=[dict(year=0,energy_kwh=0,revenue_eur=0,opex_eur=0,replacement_eur=0,decommissioning_eur=0,
        investment_eur=capex,net_cashflow_eur=-capex,discounted_cashflow_eur=-capex,cumulative_eur=-capex,discounted_cumulative_eur=-capex)]
    for year in range(1,params.years+1):
        energy=energy_kwh*(1-degradation)**(year-1)
        revenue=energy/1000*price
        opex=kwp*params.opex_eur_kwp_year+params.additional_opex_eur_year
        replacement=kwp*params.replacement_eur_kwp if year==params.replacement_year else 0
        ending=params.decommissioning_eur if year==params.years else 0
        costs=opex+replacement+ending;net=revenue-costs;factor=(1+rate)**year
        cumulative+=net;discounted+=net/factor;cost_pv+=costs/factor;energy_pv+=energy/factor
        rows.append(dict(year=year,energy_kwh=energy,revenue_eur=revenue,opex_eur=opex,replacement_eur=replacement,
            decommissioning_eur=ending,investment_eur=0,net_cashflow_eur=net,discounted_cashflow_eur=net/factor,
            cumulative_eur=cumulative,discounted_cumulative_eur=discounted))
    simple=payback([r['cumulative_eur'] for r in rows]);actualized=payback([r['discounted_cumulative_eur'] for r in rows])
    return dict(capex_eur=capex,lcoe_eur_mwh=cost_pv/energy_pv*1000 if energy_pv>0 else None,
        npv_eur=discounted,payback_years=simple,discounted_payback_years=actualized,
        net_gain_eur=cumulative,roi_percent=cumulative/capex*100 if capex>0 else None,
        payback_reversed=simple is not None and any(r['cumulative_eur']<0 for r in rows if r['year']>math.ceil(simple)),
        discounted_payback_reversed=actualized is not None and any(r['discounted_cumulative_eur']<0 for r in rows if r['year']>math.ceil(actualized)),
        first_year_revenue_eur=rows[1]['revenue_eur'],first_year_opex_eur=rows[1]['opex_eur'],
        lifetime_energy_mwh=sum(r['energy_kwh'] for r in rows)/1000,rows=rows)


def acquisition_summary(project, scenario, scenario_index, manual):
    if manual:
        return {'status':'manual'}
    cost=scenario.get('cost') or {}
    low,high=cost.get('land_low'),cost.get('land_high')
    referenced,priced=set(),set()
    if scenario_index==2:
        for row in project.get('parcels') or []:
            reference=row.get('reference_cadastrale') or row.get('parcelle')
            if not reference:
                continue
            referenced.add(reference)
            if row.get('achat_bas_eur') is not None or row.get('achat_estime_eur') is not None:
                priced.add(reference)
    status='missing'
    if scenario.get('land_included'):
        status='included'
    elif priced or any(isinstance(value,(int,float)) and value>0 for value in (low,high)):
        status='partial'
    return dict(status=status,low_eur=low,high_eur=high,priced_parcels=len(priced),total_parcels=len(referenced))


def calculate_economics(project,params):
    envelope=dict(assumptions=params.model_dump(),currency='EUR',price_basis='constant_euros',
        method_version=1,notes=['Simulation de projet avant impôts, sans emprunt, subvention ni valeur de revente.',
            'Prix de vente et charges : hypothèses modifiables, pas un tarif garanti. Production vendue intégralement.',
            'Taux d’actualisation réel ; prix et charges constants en euros d’aujourd’hui. Flux en fin d’année.',
            'Production initiale issue de la moyenne météo du scénario, puis dégradée selon le taux saisi.'])
    scenarios=project.get('scenarios') or []
    if project.get('kind')!='analysis' or params.scenario_index>=len(scenarios):
        return {**envelope,'status':'unavailable','reason':'Lancez une analyse solaire avant la simulation économique.'}
    scenario=scenarios[params.scenario_index];summary=project.get('summary') or {}
    energy=scenario.get('energy')
    if scenario.get('unavailable') or not isinstance(energy,(int,float)) or not math.isfinite(energy) or energy<=0:
        return {**envelope,'status':'unavailable','reason':'Aucune production exploitable pour ce scénario. Choisissez un autre scénario ou relancez la recherche.'}
    if params.scenario_index==2:
        placed=summary.get('placed_m2')
        if not isinstance(placed,(int,float)) or placed<=1:
            return {**envelope,'status':'unavailable','reason':'Aucune emprise PV calculée pour le scénario parcellaire.'}
        density=(project.get('config') or {}).get('density',210)
        kwp=placed*density/1000 if isinstance(density,(int,float)) and math.isfinite(density) else 0
        if placed<(summary.get('surface') or placed)-1:envelope['notes'].append('Implantation partielle : seuls les panneaux effectivement placés sont évalués.')
    else:
        kwp=summary.get('kwp') or 0
        envelope['notes'].append('Scénario de présélection : implantation parcellaire non démontrée pour cette simulation.')
    if not isinstance(kwp,(int,float)) or not math.isfinite(kwp) or kwp<=0:
        return {**envelope,'status':'unavailable','reason':'Puissance installée non disponible pour ce scénario.'}
    cost=scenario.get('cost') or {};low=cost.get('low');high=cost.get('high')
    envelope['acquisition']=acquisition_summary(project,scenario,params.scenario_index,params.capex_override_eur is not None)
    if params.capex_override_eur is not None:
        low=high=params.capex_override_eur
        envelope['notes'].append('Investissement initial manuel : il remplace le CAPEX du modèle. Vérifiez qu’il couvre tous les postes nécessaires.')
    elif not all(isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in (low,high)) or high<low:
        return {**envelope,'status':'incomplete','reason':'Le CAPEX complet n’est pas disponible. Renseignez un investissement manuel fondé sur un devis, ou complétez le raccordement.'}
    if params.scenario_index==2 and (project.get('collector') or {}).get('connections')==0:
        envelope['notes'].append('Raccordement non identifié dans l’analyse : cette simulation reste conditionnée à sa faisabilité et à son devis.')
    central=simulate((low+high)/2,energy,kwp,params)
    optimistic=simulate(low,energy,kwp,params);pessimistic=simulate(high,energy,kwp,params)
    sensitivity=[dict(price_eur_mwh=params.sale_price_eur_mwh*factor,
        **{k:v for k,v in simulate((low+high)/2,energy,kwp,params,params.sale_price_eur_mwh*factor).items() if k in {'npv_eur','payback_years'}}) for factor in (.8,1,1.2)]
    return {**envelope,'status':'calculated','scenario_name':scenario.get('name'),'installed_kwp':kwp,
        'first_year_energy_mwh':energy/1000,'capex_source':'manual' if params.capex_override_eur is not None else 'analysis_range',
        'central':central,'optimistic':optimistic,'pessimistic':pessimistic,'sensitivity':sensitivity}

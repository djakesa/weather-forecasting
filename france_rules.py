"""French legal screening rules, explicit evidence and conservative exclusions."""
from datetime import date
from shapely.geometry import Point,shape
from shapely.ops import unary_union
import france_pv as core

URBANISM='https://www.legifrance.gouv.fr/codes/section_lc/LEGITEXT000006074075/LEGISCTA000047298099/'
AGRIVOLTAIC='https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000047298015/'
ACTIVITY='https://www.legifrance.gouv.fr/codes/section_lc/LEGITEXT000023983208/LEGISCTA000049387803/'


def legal_perimeter(data,area):
    if data is None:return Point().buffer(0),[]
    if data.get('type')!='FeatureCollection' or not isinstance(data.get('features'),list):raise ValueError('Périmètre légal : importer un FeatureCollection GeoJSON.')
    geometries=[];evidence=[]
    for feature in data['features']:
        if not isinstance(feature,dict) or not isinstance(feature.get('geometry'),dict):raise ValueError('Chaque périmètre légal doit fournir une géométrie GeoJSON.')
        props=feature.get('properties') or {}
        for key in ('legal_basis','source_url','reference','approval_date'):
            if not props.get(key):raise ValueError(f'Périmètre légal : propriété {key} obligatoire.')
        if props['legal_basis'] not in {'document_cadre_L111_29','autorisation_urbanisme'}:raise ValueError('Base légale attendue : document_cadre_L111_29 ou autorisation_urbanisme.')
        from urllib.parse import urlparse
        parsed=urlparse(str(props['source_url']))
        if parsed.scheme!='https' or not (parsed.hostname and (parsed.hostname.endswith('.gouv.fr') or parsed.hostname=='gouv.fr')):
            raise ValueError('Le périmètre doit référencer une publication officielle HTTPS en .gouv.fr.')
        approved=date.fromisoformat(str(props['approval_date']))
        if approved>date.today():raise ValueError('La date du périmètre légal ne peut pas être future.')
        geom=shape(feature.get('geometry'))
        if geom.is_empty or geom.geom_type not in {'Polygon','MultiPolygon'} or not geom.is_valid:raise ValueError('Le périmètre légal doit contenir des polygones valides.')
        w,s,e,n=geom.bounds
        if not (-180<=w<=e<=180 and -90<=s<=n<=90):raise ValueError('Périmètre légal attendu en WGS84.')
        clipped=core.metric(geom).intersection(area)
        if not clipped.is_empty:
            geometries.append(clipped);evidence.append({key:props[key] for key in ('legal_basis','source_url','reference','approval_date')})
    return unary_union(geometries),evidence


def apply_french_rules(config,area,regulation):
    excluded=regulation['exclusions'];checks=list(regulation['checks']);features=list(regulation['features'])
    decisions=[]
    for feature in features:
        props=feature.get('properties') or {};kind=props.get('constraint')
        if (kind=='Zone naturelle GPU' and config.avoid_natural_zones) or (kind in {'Servitude GPU','Prescription GPU'} and config.avoid_servitudes):
            excluded=excluded.union(core.metric(shape(feature['geometry'])))
    if config.avoid_natural_zones:decisions.append('Zones naturelles GPU évitées par politique de présélection ; ce n’est pas une interdiction générale déduite du seul code N.')
    if config.avoid_servitudes:decisions.append('Assiettes surfaciques de servitudes et prescriptions GPU évitées par prudence ; leurs règlements particuliers restent à lire.')
    allowed,evidence=legal_perimeter(config.legal_areas_geojson,area)
    if config.siting_mode=='ground':
        # Outside this supplied legal scope we do not invent eligibility for agricultural land.
        excluded=excluded.union(area.difference(allowed))
        checks.append(dict(name='Périmètre légal PV au sol',status='declared' if evidence else 'missing',source_url=URBANISM,note='Emprise limitée au périmètre officiel importé. Authenticité, champ du document et autorisations à confirmer.' if evidence else 'Périmètre admissible non fourni dans ce secteur : aucun PV au sol n’y est proposé. Importer le document-cadre applicable ou un périmètre autorisé avec sa référence.',evidence=evidence))
        decisions.append('PV au sol limité aux polygones officiels importés ; aucune admissibilité nationale n’est déduite du cadastre ou du RPG.')
    else:
        checks.append(dict(name='Qualification agrivoltaïque — L314-36',status='unknown',source_url=AGRIVOLTAIC,note='Démontrer au moins un service agricole, une production significative, un revenu agricole durable, le caractère principal de l’agriculture et la réversibilité. Le modèle ne certifie pas ces conditions.',declared_service=config.agricultural_service))
        kwp=config.surface*config.density/1000
        coverage=config.legal_coverage_percent
        blocked=kwp>10000 and not config.proven_technology and coverage is not None and coverage>40
        if blocked:excluded=excluded.union(area)
        checks.append(dict(name='Couverture réglementaire — R314-118 / R314-119',status='blocked' if blocked else 'declared' if coverage is not None else 'unknown',source_url=ACTIVITY,note='Couverture déclarée supérieure à 40 % pour un projet de plus de 10 MWc sans régime de technologie éprouvée déclaré : emprise exclue.' if blocked else 'Le plafond de 40 % concerne les installations de plus de 10 MWc hors régime de technologies éprouvées. La couverture réglementaire repose sur la projection maximale et la parcelle agricole ; elle diffère du ratio géométrique utilisé pour dimensionner le terrain.',declared_percent=coverage))
    return {**regulation,'checks':checks,'features':features,'exclusions':excluded,'decisions':decisions,'legal_mode':config.siting_mode,'rules_version':'2026-11-16','legal_coverage':allowed,'legal_evidence':evidence}

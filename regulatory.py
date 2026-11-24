"""Official French spatial screening, with provenance and explicit limits."""
from datetime import datetime,timezone
import json
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union
from shapely import STRtree, make_valid
from shapely.errors import ShapelyError
import france_pv as core


def validated_features(data,source,*,allow_non_polygon=False):
    if not isinstance(data,dict) or data.get('error') or data.get('type')!='FeatureCollection' or not isinstance(data.get('features'),list):raise ValueError(f'{source} : réponse invalide')
    rows=data['features']
    metadata=data.get('properties')
    if data.get('exceededTransferLimit') or (isinstance(metadata,dict) and metadata.get('exceededTransferLimit')):raise ValueError(f'{source} : réponse tronquée')
    for key in ('numberMatched','totalFeatures'):
        if str(data.get(key)).isdigit() and int(data[key])>len(rows):raise ValueError(f'{source} : réponse tronquée')
    validated=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('geometry'),dict):raise ValueError(f'{source} : géométrie manquante')
        if row.get('properties') is not None and not isinstance(row['properties'],dict):
            raise ValueError(f'{source} : propriétés GeoJSON invalides')
        try:
            geometry=shape(row['geometry'])
        except (KeyError, TypeError, ValueError, ShapelyError) as exc:
            raise ValueError(f'{source} : géométrie GeoJSON invalide') from exc
        if geometry.is_empty or (not allow_non_polygon and geometry.geom_type not in {'Polygon','MultiPolygon'}):raise ValueError(f'{source} : géométrie manquante ou invalide')
        if allow_non_polygon and not geometry.is_valid:raise ValueError(f'{source} : géométrie invalide')
        if not geometry.is_valid:
            repaired=make_valid(geometry)
            def surfaces(geom):
                if geom.geom_type in {'Polygon','MultiPolygon'}:return [geom]
                return [part for child in getattr(geom,'geoms',[]) for part in surfaces(child)]
            geometry=unary_union(surfaces(repaired))
            if geometry.is_empty or not geometry.is_valid or geometry.area<=0:raise ValueError(f'{source} : géométrie irréparable')
            row={**row,'geometry':mapping(geometry),'properties':{**(row.get('properties') or {}),'geometry_repaired':True}}
        w,s,e,n=geometry.bounds
        if not (-180<=w<=e<=180 and -90<=s<=n<=90):raise ValueError(f'{source} : coordonnées hors WGS84')
        validated.append(row)
    return validated


GPU_LAYERS = (
    ('zone-urba', 'Zonage d’urbanisme GPU'),
    ('secteur-cc', 'Secteurs des cartes communales GPU'),
    ('assiette-sup-s', 'Servitudes surfaciques GPU'),
    ('assiette-sup-l', 'Servitudes linéaires GPU'),
    ('assiette-sup-p', 'Servitudes ponctuelles GPU'),
)


def regulatory_context(area, country='France', progress=lambda _: None):
    if country != 'France':
        raise ValueError('Le GPU couvre uniquement la France.')
    features, checks = [], []
    bounds = box(*core.geographic(area).bounds)
    query = {'geom': json.dumps(mapping(bounds), separators=(',', ':'))}
    for endpoint, label in GPU_LAYERS:
        url = 'https://apicarto.ign.fr/api/gpu/' + endpoint
        progress(f'GPU : {label}…')
        try:
            rows = validated_features(
                core.api(url, query, progress=progress), label,
                allow_non_polygon=endpoint in {'assiette-sup-l', 'assiette-sup-p'},
            )
            local = []
            for row in rows:
                geometry = core.metric(shape(row['geometry'])).intersection(area)
                if geometry.is_empty:
                    continue
                props = row.get('properties') or {}
                local.append({**row, 'geometry': mapping(core.geographic(geometry)), 'properties': {
                    **props, 'constraint': label, 'layer': endpoint, 'source_url': url,
                }})
            features.extend(local)
            checks.append(dict(name=label, status='checked' if local else 'unknown',
                               count=len(local), source_url=url,
                               note='Consulter les documents applicables au terrain.'))
        except (ValueError, core.requests.RequestException) as exc:
            checks.append(dict(name=label, status='unavailable', source_url=url, note=str(exc)))
    return dict(status='partial', checked_at=datetime.now(timezone.utc).isoformat(),
                checks=checks, features=features)


def annotate_parcels(features, area, farmland, regulation):
    gpu = regulation['features']
    index = STRtree([core.metric(shape(row['geometry'])) for row in gpu])
    parcels = []
    for row in features:
        observed = core.metric(shape(row['geometry'])).buffer(0).intersection(area)
        if observed.area <= 0:
            continue
        matches = []
        for position in sorted(index.query(observed, predicate='intersects')):
            props = gpu[position]['properties']
            matches.append({key: props[key] for key in
                            ('layer', 'gid', 'libelle', 'typezone', 'typesect', 'suptype',
                             'nomass', 'idass', 'idurba', 'urlfic', 'source_url')
                            if key in props})
        overlap = observed.intersection(farmland).area if farmland is not None else None
        props = {**(row.get('properties') or {}), 'gpu_intersections': matches,
                 'surface_agricole_zone_m2': overlap,
                 'part_agricole_pct': 100 * overlap / observed.area if overlap is not None else None}
        parcels.append({**row, 'properties': props})
    return parcels

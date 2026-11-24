"""Screen solar sectors for land availability and grid access."""
import math

from shapely.geometry import Point, shape
from shapely.ops import unary_union

from backend.domain.costs import capex, connection
from backend.domain.geometry import geographic, metric
from backend.domain.inventory import apply_verified_inventory
from connection_preferences import apply_voltage_preferences, capex_for_connection

from .ports import SiteSources, SourceUnavailable


def candidate_centers(preferred,samples,loss,radius,maximum=3,boundary=None,proxy_distance=0):
    centers=[preferred]
    if maximum<=1:return centers
    best=float(samples['yield'].max())
    eligible=samples[samples['yield']>=best*(1-loss/100)].sort_values('yield',ascending=False)
    for row in eligible.to_dict('records'):
        point=Point(row['x'],row['y'])
        if all(point.distance(other)>2*radius*1000 for other in centers):
            centers.append(point)
        if len(centers)>=maximum:break
    # A sparse weather budget must not restrict land screening to one mountain.
    # Nearby sectors reuse a labelled weather estimate, never a new observation.
    if boundary is not None and len(centers)<maximum:
        for scale in (2.2,4.4):
            for direction in range(8):
                angle=direction*math.pi/4
                point=Point(preferred.x+scale*radius*1000*math.cos(angle),preferred.y+scale*radius*1000*math.sin(angle))
                if not boundary.covers(point) or any(point.distance(other)<=2*radius*1000 for other in centers):continue
                nearest=min(samples.to_dict('records'),key=lambda r:point.distance(Point(r['x'],r['y'])))
                if point.distance(Point(nearest['x'],nearest['y']))<=proxy_distance and nearest['yield']>=best*(1-loss/100):
                    centers.append(point)
                if len(centers)>=maximum:return centers
    return centers


def imported_features(config,area):
    data=config.cadastral_geojson
    if data.get('type')!='FeatureCollection':raise ValueError('Importer un FeatureCollection GeoJSON WGS84.')
    features=[]
    for i,feature in enumerate(data.get('features',[])):
        geom=shape(feature.get('geometry'))
        if geom.is_empty or geom.geom_type not in {'Polygon','MultiPolygon'}:raise ValueError('Le cadastre doit contenir des polygones.')
        w,s,e,n=geom.bounds
        if not (-180<=w<=e<=180 and -90<=s<=n<=90):raise ValueError('Coordonnées cadastrales attendues en WGS84.')
        if metric(geom).intersects(area):
            props={**(feature.get('properties') or {})}
            props.setdefault('idu',feature.get('id') or props.get('id') or f'import/{i}')
            features.append({**feature,'properties':props})
    return features


def evaluate_sites(config,boundary,preferred,samples,assets,progress,parcels,terrain,network, resolve_country, *, sources: SiteSources):
    proxy_distance=2500 if config.start>=2025 else 9000
    centers=candidate_centers(preferred,samples,config.loss,config.parcel_radius,maximum=config.max_sectors,boundary=boundary,proxy_distance=proxy_distance)
    outcomes=[]; all_assets={a['id']:a for a in apply_voltage_preferences(assets,config.connection_voltages_kv)}; failures=[]
    def solar_value(center):
        return min(samples.to_dict('records'),key=lambda row:center.distance(Point(row['x'],row['y'])))['yield']
    centers.sort(key=solar_value,reverse=True)
    connection_radius=(config.parcel_radius+config.move)*1000
    def local_assets(center):
        return [a for a in all_assets.values() if a['geometry'].distance(center)<=connection_radius]
    def site_link(outcome):return connection(outcome['center'],local_assets(outcome['center']))
    initial_count=len(centers)
    def search_centers():
        yield from centers
        # Failed first choices are not proof that the search circle is unusable.
        # Reuse already evaluated weather points, relaxing only the solar preference.
        if config.max_sectors>1 and not any(o['computed'] and o['missing'] is not None and o['missing']<=1 and site_link(o) is not None for o in outcomes):
            pool=candidate_centers(preferred,samples,100,config.parcel_radius,maximum=len(samples)+1,boundary=boundary,proxy_distance=proxy_distance)
            extra=[]
            for point in pool:
                if all(point.distance(other)>2*config.parcel_radius*1000 for other in centers+extra):extra.append(point)
                if len(extra)>=config.max_sectors:break
            if extra:
                extra.sort(key=solar_value,reverse=True)
                progress(f'Recherche élargie : {len(extra)} autres secteurs ; préférence solaire assouplie, sans nouvel appel météo.')
                centers.extend(extra)
                yield from extra
    for index,center in enumerate(search_centers()):
        area=boundary.intersection(center.buffer(config.parcel_radius*1000))
        if area.is_empty:continue
        geo=geographic(center);country=resolve_country(geo.y,geo.x,config.country)
        outcome=dict(index=index+1,center=center,area=area,country=country,features=[],footprints=[],missing=None,computed=False,metadata={},warnings=[],regulatory=None)
        outcome['search_expanded']=index>=initial_count
        zones=sources.grid_context(area.buffer(1000),progress)
        outcome['grid_constraints']=sources.public_grid(zones)
        if zones['status']=='unavailable':outcome['warnings'].append('Contraintes Enedis non consultées ; capacité réseau inconnue.')
        elif zones.get('stale'):outcome['warnings'].append('Référentiel de contrainte Enedis de plus de 90 jours ; actualisation à vérifier.')
        progress(f'Secteur {index+1}/{len(centers)} : parcelles, terrain et contraintes…')
        if index:
            try:
                nearby,_,_=network(boundary.intersection(center.buffer((config.parcel_radius+1)*1000)),config.country,progress=progress,accepted_voltages_kv=config.connection_voltages_kv)
                if config.inventory_csv:
                    nearby,_=apply_verified_inventory(nearby,config.inventory_csv)
                nearby=apply_voltage_preferences(nearby,config.connection_voltages_kv)
                for asset in nearby:
                    previous=all_assets.get(asset['id'])
                    if previous is None:all_assets[asset['id']]=asset
                    else:
                        # One exact OSM way may be clipped into different MVT tiles.
                        # Retain its newly observed geometry at the next solar site.
                        previous['geometry']=previous['geometry'].union(asset['geometry'])
            except (ValueError, SourceUnavailable) as exc:
                outcome['warnings'].append(f'Réseau indisponible dans le secteur {index+1} : {exc}')
        # Apply each local zone to all known targets before evaluating this sector.
        # Previously stamped targets retain their constraint outside this local extract.
        stamped=sources.annotate_grid(list(all_assets.values()),zones)
        for asset in stamped:
            if asset['enedis_zone_statuses']:all_assets[asset['id']]=asset
        try:
            features=imported_features(config,area) if config.cadastral_geojson is not None else parcels(area,country,progress=progress)
            outcome['features']=features;outcome['metadata']['parcelles_recues']=len(features)
            if not features:
                outcome['warnings'].append('Aucune parcelle cadastrale disponible dans ce secteur ; couverture ou autre emplacement à vérifier.')
                outcomes.append(outcome);continue
        except (ValueError, SourceUnavailable) as exc:
            outcome['warnings'].append(f'Cadastre indisponible dans le secteur {index+1} : {exc}')
            outcomes.append(outcome);continue
        try:
            terrain_options=dict(progress=progress,metadata=outcome['metadata'])
            if config.rpg_geojson is not None or config.rpg_year!=2024:
                terrain_options['mode']='ground'
            excluded,farmland=terrain(area,country,**terrain_options)
            if config.rpg_geojson is not None:
                farmland=sources.rpg_overlap(config.rpg_geojson['features'],area)
                outcome['metadata'].update(agricultural_year=config.rpg_year,rpg_status='checked')
            elif config.rpg_year!=2024:
                farmland=sources.read_rpg(area,progress,outcome['metadata'],config.rpg_year)
            regulation=sources.regulatory_context(area,country,progress)
            rpg_status=outcome['metadata'].get('rpg_status','checked')
            rpg_check=dict(
                name=f'RPG {config.rpg_year}',status=rpg_status,
                source_url='https://geoservices.ign.fr/rpg',
                note='Recouvrement avec les parcelles agricoles déclarées. Indicateur à vérifier lors de l’étude du projet.',
            )
            regulation={**regulation,'checks':[*regulation['checks'],rpg_check]}
            outcome['regulatory']=regulation
            features=sources.annotate_parcels(features,area,farmland,regulation)
            outcome['features']=features
            if any(check['status']=='unavailable' for check in regulation['checks']):
                outcome['warnings'].append('Données territoriales incomplètes : une couche GPU ou RPG est indisponible.')
            physical=excluded
            electrical=unary_union([a['geometry'].buffer(15) for a in all_assets.values()])
            excluded=excluded.union(electrical)
            if all(v<=50 for v in config.connection_voltages_kv):
                excluded=excluded.union(zones['exclusions'])
            available=unary_union([metric(shape(f['geometry'])).buffer(0) for f in features]).intersection(area)
            diagnostics=[]
            for label,mask in [('Obstacles physiques',physical),('Marge autour des ouvrages électriques',electrical),('Saturation Enedis HTA/BT',zones['exclusions'] if all(v<=50 for v in config.connection_voltages_kv) else Point().buffer(0))]:
                before=available.area;available=available.difference(mask)
                diagnostics.append(dict(reason=label,removed_m2=max(0,before-available.area),remaining_m2=available.area))
            outcome['metadata']['land_diagnostics']=diagnostics
            footprints,missing=sources.allocate(features,area,excluded,area,center,config.surface,config.coverage,rpg_indicator=True)
            outcome.update(footprints=footprints,missing=missing,computed=True)
            if not footprints:
                main=max(diagnostics,key=lambda item:item['removed_m2'])
                outcome['warnings'].append(f"Secteur écarté : {main['reason'].lower()} ({main['removed_m2']:,.0f} m² retirés)." if main['removed_m2']>1 else 'Emprises restantes trop petites après les marges de 5 m en limite parcellaire.')
        except (ValueError, SourceUnavailable) as exc:
            outcome['warnings'].append(f'Contexte du terrain indisponible : {exc}')
        outcomes.append(outcome)
        if outcome['computed'] and outcome['missing']<=1 and site_link(outcome) is not None and not any(c['status']=='unavailable' for c in (outcome.get('regulatory') or {}).get('checks',[])):
            progress(f'Secteur {index+1} retenu : surface complète et cible réseau identifiée. Arrêt des recherches de secteurs moins irradiés.')
            break
    # Feasibility first; among complete connected sites, prefer solar resource.
    def score(outcome):
        center=outcome['center'];link=site_link(outcome)
        nearest=min(samples.to_dict('records'),key=lambda row:center.distance(Point(row['x'],row['y'])))
        cost=capex(config.surface*config.density/1000,link['distance_m'] if link else None,config.assumptions())
        cost=capex_for_connection(cost,link)
        metric=cost['high']/(nearest['yield']*config.surface*config.density/1000) if cost['high'] is not None else float('inf')
        unavailable=sum(c['status']=='unavailable' for c in (outcome.get('regulatory') or {}).get('checks',[]))
        grid=outcome['grid_constraints']
        distribution=min(link.get('voltage_selection_matched_v') or [min(config.connection_voltages_kv)*1000])<=50000 if link else min(config.connection_voltages_kv)<=50
        grid_unknown=distribution and (grid['status']=='unavailable' or grid.get('coverage_share',0)<.999 or grid.get('stale',False))
        grid_tension=distribution and 'EN_TENSION' in grid.get('statuses',[])
        missing=round(outcome['missing'],1) if outcome['missing'] is not None else config.surface
        feasible=outcome['computed'] and missing<=1 and link is not None
        return (not feasible,not outcome['computed'],missing,link is None,unavailable,-nearest['yield'],grid_unknown,grid_tension,metric)
    chosen=min(outcomes,key=score)
    reports=[]
    for outcome in outcomes:
        geo=geographic(outcome['center'])
        placed=config.surface-outcome['missing'] if outcome['missing'] is not None else None
        nearest=min(samples.to_dict('records'),key=lambda r:outcome['center'].distance(Point(r['x'],r['y'])))
        source=Point(nearest.get('weather_source_x',nearest['x']),nearest.get('weather_source_y',nearest['y']))
        link=site_link(outcome)
        outcome['connection_found']=link is not None
        decision='source_unavailable' if not outcome['computed'] else 'insufficient_land' if outcome['missing']>1 else 'no_connection' if link is None else 'accepted'
        outcome['decision']=decision
        if decision=='no_connection':outcome['warnings'].append('Surface disponible, mais aucune cible réseau de tension compatible identifiée dans le périmètre local ; recherche du secteur suivant.')
        reports.append(dict(index=outcome['index'],location=[geo.y,geo.x],chosen=outcome is chosen and decision=='accepted',placed_m2=placed,status='calculated' if placed and placed>1 else 'no_usable_land' if outcome['computed'] else 'unavailable',decision=decision,connection_found=link is not None,connection_radius_m=connection_radius,weather_distance_m=outcome['center'].distance(source),issues=outcome['warnings'],search_expanded=outcome['search_expanded'],solar_loss_percent=100*(1-nearest['yield']/float(samples['yield'].max())),land_diagnostics=outcome['metadata'].get('land_diagnostics',[])))
    if chosen['search_expanded'] and chosen['footprints']:
        chosen['warnings'].append('Secteur trouvé après élargissement et assouplissement de la préférence solaire ; écart d’irradiance indiqué dans la comparaison.')
    chosen['reports']=reports;chosen['assets']=list(all_assets.values());chosen['connection_assets']=local_assets(chosen['center'])
    return chosen

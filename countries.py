"""France-only adapters for administrative, cadastral and network sources."""
from shapely.geometry import Point
import france_pv as core
import land_prices

COUNTRIES={"France":dict(model=core.METEO_MODEL,default=(43.61,3.88),bounds=(41.,51.5,-5.5,10.),agricultural="RPG 2024")}


def require_france(country):
    if country!='France':raise ValueError('Seule la France métropolitaine, Corse comprise, est prise en charge.')


def context_country(latitude,longitude,selection):
    require_france(selection)
    return 'France'


def search_area(lat,lon,radius,country='France'):
    require_france(country)
    if not .1<=radius<=1000:raise ValueError('Rayon attendu entre 0,1 et 1 000 km.')
    return core.gps_area(lat,lon,radius)


def parcels(area,country='France',progress=lambda _:None):
    require_france(country)
    return core.cadastral(area)


def terrain(area,country='France',progress=lambda _:None,metadata=None,mode='agrivoltaic'):
    require_france(country)
    obstacles = core.terrain_obstacles(area, progress, metadata)
    if mode=='ground':
        return obstacles, None
    return obstacles, core.read_rpg(area, progress, metadata)


def electrical_network(area,country='France',progress=lambda _:None,accepted_voltages_kv=None):
    require_france(country)
    from openinfra_network import network_assets
    errors=[]
    try:
        assets=network_assets(area,progress)
    except (ValueError,core.requests.RequestException) as exc:
        assets=[];errors.append(str(exc))
        progress(f'Open Infrastructure Map indisponible : {exc}')
    from connection_preferences import apply_voltage_preferences
    progress('Réseau : localisation officielle des ouvrages Enedis…')
    try:assets.extend(core.enedis_assets(area,progress))
    except (ValueError,core.requests.RequestException) as exc:
        errors.append(str(exc));progress(f'Enedis indisponible : {exc}')
    candidates=assets if accepted_voltages_kv is None else apply_voltage_preferences(assets,accepted_voltages_kv)
    if not assets or (all('geometry' in a for a in assets) and core.connection(area.representative_point(),candidates) is None):
        for label,source in [('IGN BD TOPO',core.ign_electrical_assets)]:
            progress(f'Réseau : complément officiel {label}…')
            try:assets.extend(source(area,progress))
            except (ValueError,core.requests.RequestException) as exc:
                errors.append(str(exc));progress(f'{label} indisponible : {exc}')
        if not assets and errors:raise ValueError('Sources réseau incomplètes : '+' ; '.join(errors))
    return assets,Point().buffer(0),Point().buffer(0)


def land_reference(point,country='France',department_code=None):
    require_france(country)
    return land_prices.agricultural_price(point=point,department_code=department_code)

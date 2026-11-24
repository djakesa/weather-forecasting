"""Wire the solar use case to the French data providers."""
from functools import partial

from countries import (
    COUNTRIES, context_country as resolve_context_country,
    electrical_network as country_network, land_reference as country_price,
    parcels as country_parcels, search_area as country_area, terrain as country_terrain,
)
from electrical_data import enrich_transformers
from france_pv import SolarEvaluator, solar_model
from land_prices import local_land_prices
from spatial_sampling import sample_solar as search_solar

from .application.analyze import analyze as run_analysis
from .adapters.providers import provider
from .application.ports import AnalysisSources
from .siting import evaluate_sites


class WeatherSession:
    def __init__(self, *args, **kwargs):
        self.evaluator = SolarEvaluator(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.evaluator, name)

    def __call__(self, points):
        return provider(self.evaluator, points)


def analyze(config, progress=lambda _: None):
    sources = AnalysisSources(
        countries=COUNTRIES,
        solar_model=solar_model,
        area=country_area,
        resolve_country=resolve_context_country,
        solar_evaluator=WeatherSession,
        sample_solar=search_solar,
        land_reference=country_price,
        network=partial(provider, country_network),
        enrich_transformers=partial(provider, enrich_transformers),
        evaluate_sites=evaluate_sites,
        parcels=country_parcels,
        terrain=country_terrain,
        local_land_prices=local_land_prices,
    )
    return run_analysis(config, progress, sources)

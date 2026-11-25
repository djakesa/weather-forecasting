"""Dependencies required by the solar analysis use case."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass


class SourceUnavailable(RuntimeError):
    """A provider could not deliver data."""


@dataclass(frozen=True, slots=True)
class AnalysisSources:
    countries: Mapping
    solar_model: Callable
    area: Callable
    resolve_country: Callable
    solar_evaluator: Callable
    sample_solar: Callable
    land_reference: Callable
    network: Callable
    enrich_transformers: Callable
    evaluate_sites: Callable
    parcels: Callable
    terrain: Callable
    local_land_prices: Callable


@dataclass(frozen=True, slots=True)
class SiteSources:
    grid_context: Callable
    annotate_grid: Callable
    public_grid: Callable
    regulatory_context: Callable
    annotate_parcels: Callable
    rpg_overlap: Callable
    read_rpg: Callable
    allocate: Callable
    elevations: Callable

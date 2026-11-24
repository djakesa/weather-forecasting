"""Wire sector screening to the French data providers."""
from functools import partial

import france_pv
from countries import context_country
from enedis_constraints import annotate as annotate_grid, context as grid_context, public as public_grid
from regulatory import annotate_parcels, regulatory_context

from .adapters.providers import provider
from .application.ports import SiteSources
from .application.siting import candidate_centers, evaluate_sites as screen_sites, imported_features


def evaluate_sites(
    config, boundary, preferred, samples, assets, progress, parcels, terrain, network,
    resolve_country=context_country,
):
    sources = SiteSources(
        grid_context=grid_context,
        annotate_grid=annotate_grid,
        public_grid=public_grid,
        regulatory_context=partial(provider, regulatory_context),
        annotate_parcels=annotate_parcels,
        rpg_overlap=france_pv.rpg_overlap,
        read_rpg=partial(provider, france_pv.read_rpg),
        allocate=france_pv.allocate,
    )
    return screen_sites(
        config, boundary, preferred, samples, assets, progress,
        partial(provider, parcels), partial(provider, terrain), partial(provider, network),
        resolve_country, sources=sources,
    )

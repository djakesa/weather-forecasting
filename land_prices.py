"""Official agricultural market references, never an appraisal of a parcel."""
from __future__ import annotations

import csv
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date
from functools import lru_cache
from statistics import fmean

import pandas as pd
import requests

from shapely.geometry import Point
from shapely.prepared import prep

from france_pv import CACHE, api, geographic, metric
from backend.domain.costs import LOCAL_PRICE_MODE, price_envelope

PRICE_YEAR = 2025
PRICE_SOURCE = "https://www.agreste.agriculture.gouv.fr/agreste-web/disaron/Chd2609/detail/"
PRICE_FILE = "https://www.agreste.agriculture.gouv.fr/agreste-web/download/publication/publie/Chd2609/DonneesAssociees-cd2026-9_VVT-2025_Publication-LIBRE.xlsx"
DVF_SOURCE = "https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees"
DVF_FILES = "https://files.data.gouv.fr/geo-dvf/latest/csv"
AGRICULTURAL_TYPES = {"T", "P", "VE", "VI"}


def bare_land_sales(rows):
    """Read complete groups before selecting comparable sales."""
    mutations = defaultdict(set)
    for row in rows:
        if row["id_mutation"]:
            mutations[row["id_mutation"]].add(tuple(row[key] for key in (
                "nature_mutation", "id_parcelle", "numero_disposition", "code_type_local",
                "type_local", "surface_reelle_bati", "nombre_lots", "numero_volume",
                "code_nature_culture", "nature_culture", "valeur_fonciere", "surface_terrain",
                "longitude", "latitude",
            )))
    for entries in mutations.values():
        if len(entries) != 1:
            continue
        (sale, parcel, _, local_code, local_type, built, lots, volume, category, label,
         value, area, longitude, latitude), = entries
        if sale not in {"Vente", "Vente terrain à bâtir"} or not parcel or local_code or local_type or volume or not category:
            continue
        try:
            if float(built or 0) != 0 or float(lots or 0) != 0:
                continue
            value, area, lon, lat = map(float, (value, area, longitude, latitude))
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(x) for x in (value, area, lon, lat)) and value > 0 and area > 0 and -5.5 <= lon <= 10 and 41 <= lat <= 51.5:
            yield parcel, category, label, area, value / area, lon, lat


@lru_cache(maxsize=64)
def commune_sales(code, year, refresh_date):
    """Cache a commune-year extract for the day supplied in the cache key."""
    url = f"{DVF_FILES}/{year}/communes/{code[:2]}/{code}.csv"
    with requests.get(url, timeout=(5, 12), stream=True) as response:
        if response.status_code == 404:
            return ()
        response.raise_for_status()
        response.encoding = "utf-8-sig"
        return tuple(bare_land_sales(csv.DictReader(response.iter_lines(decode_unicode=True))))


def local_land_prices(parcels, selected_area, progress):
    """Return acquisition references by cadastral identifier, with provenance."""
    today = date.today()
    years = range(today.year - 3, today.year)
    scope = prep(selected_area)
    by_commune = defaultdict(list)
    for parcel in parcels:
        reference = parcel["reference_cadastrale"]
        commune = str(parcel.get("commune_code") or "").strip().upper()
        if not commune and re.fullmatch(r"[0-9A-Z]{14}", reference):
            commune = reference[:5]
        if commune and re.fullmatch(r"(?:[0-9]{2}|2[AB])[0-9]{3}", commune) and commune[:2] not in {"57", "67", "68"}:
            by_commune[commune].append(parcel)
    prices = {}
    for commune, subjects in by_commune.items():
        progress(f"Foncier : ventes de terrains DVF dans la commune {commune}…")
        try:
            sales = [sale for year in years for sale in commune_sales(commune, year, today)]
        except (requests.RequestException, KeyError, ValueError, csv.Error):
            continue
        in_scope = [sale for sale in sales if scope.covers(metric(Point(sale[-2], sale[-1])))]
        for subject in subjects:
            reference = subject["reference_cadastrale"]
            category = subject.get("land_category")
            historic_types = {sale[1] for sale in sales if sale[0] == reference}
            if not category and len(historic_types) == 1:
                category = historic_types.pop()
            agricultural = not category and (subject.get("part_agricole_pct") or 0) > 50
            if not category and not agricultural:
                continue
            surface = subject["surface_parcelle_m2"]
            comparable = [sale for sale in in_scope if sale[0] != reference
                          and surface / 4 <= sale[3] <= surface * 4
                          and (sale[1] in AGRICULTURAL_TYPES if agricultural else sale[1] == category)]
            if len({sale[0] for sale in comparable}) < 3:
                continue
            prices[reference] = {
                "eur_m2": fmean(sale[4] for sale in comparable),
                "count": len(comparable),
                "basis": "Terres agricoles (indicateur RPG)" if agricultural else comparable[0][2] or category,
                "commune_code": commune,
                "reference_period": f"{years.start}–{years.stop - 1}",
                "source": "DGFiP / DVF géolocalisées",
                "url": DVF_SOURCE,
            }
    return prices


def normalized_name(value):
    value = unicodedata.normalize("NFKD", str(value))
    return re.sub(r"[^A-Z0-9]", "", value.encode("ascii", "ignore").decode().upper())


@lru_cache(maxsize=128)
def parse_price_table(path, department_name):
    target = normalized_name(department_name)
    with pd.ExcelFile(path) as workbook:
        for sheet in workbook.sheet_names:
            frame = pd.read_excel(workbook, sheet_name=sheet, header=None)
            header = next((i for i, row in frame.iterrows() if any(str(v).strip() in {str(PRICE_YEAR), f"{PRICE_YEAR}.0"} for v in row.tolist())), None)
            if header is None:
                continue
            column = next(i for i, v in enumerate(frame.iloc[header].tolist()) if str(v).strip() in {str(PRICE_YEAR), f"{PRICE_YEAR}.0"})
            for _, row in frame.iloc[header + 1:].iterrows():
                if normalized_name(row.iloc[0]) == target:
                    value = pd.to_numeric(row.iloc[column], errors="coerce")
                    if pd.notna(value) and math.isfinite(float(value)) and value > 0:
                        return float(value) / 10000
    raise ValueError(f"Aucune référence agricole {PRICE_YEAR} publiée pour {department_name}.")


def agricultural_price(point=None, department_code=None):
    if department_code is None:
        p = geographic(point)
        communes = api("https://geo.api.gouv.fr/communes", {"lat": p.y, "lon": p.x, "fields": "departement"})
        if not communes:
            raise ValueError("Département du terrain non identifié.")
        department_code = communes[0]["departement"]["code"]
    department = api(f"https://geo.api.gouv.fr/departements/{department_code}")
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "agreste_terres_libres_2025.xlsx"
    if not path.exists():
        response = requests.get(PRICE_FILE, timeout=(8, 45))
        try:
            response.raise_for_status()
            # Validate the workbook before making it a persistent reference.
            from io import BytesIO
            content = response.content
            with pd.ExcelFile(BytesIO(content)) as workbook:
                if not workbook.sheet_names:
                    raise ValueError("Classeur agricole vide")
            path.write_bytes(content)
        finally:
            response.close()
    value = parse_price_table(path, department["nom"])
    return {"eur_m2": value, "eur_ha": value * 10000, "department": department["nom"],
            "department_code": str(department_code), "year": PRICE_YEAR,
            "currency_basis_year": 2024, "reference_period": "2023–2025",
            "basis": "Terres et prés libres non bâtis de plus de 70 ares, moyenne triennale départementale",
            "source": "Agreste / Groupe Safer-SSP", "url": PRICE_SOURCE}



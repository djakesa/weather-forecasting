import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from shapely.geometry import Point, box, mapping

from france_pv import cadastral, cadastral_carto, geographic, metric, capex, asset_table, allocate, agricultural_cadastre
from land_prices import bare_land_sales, local_land_prices, parse_price_table, price_envelope


class CadastreAndPriceTests(unittest.TestCase):
    def setUp(self):
        self.area = box(700000, 6600000, 701000, 6601000)
        self.feature = {"type": "Feature", "id": "p1", "properties": {"idu": "p1"}, "geometry": mapping(geographic(self.area))}

    def test_cadastre_uses_paginated_wfs_before_carto(self):
        with (patch("france_pv.wfs_features", return_value=[self.feature]), patch("france_pv.api") as carto):
            self.assertEqual(cadastral(self.area), [self.feature])
            carto.assert_not_called()

    def test_truncated_carto_subdivides_and_deduplicates(self):
        def fetch(*args, **kwargs):
            geom = kwargs["payload"]["geom"]
            from shapely.geometry import shape
            from france_pv import metric
            area = metric(shape(geom)).area
            return {"totalFeatures": 2 if area > 300000 else 1, "features": [self.feature]}
        with patch("france_pv.api", side_effect=fetch) as get:
            self.assertEqual(cadastral_carto(self.area), [self.feature])
            self.assertGreater(get.call_count, 1)

    def test_never_accepts_permanently_truncated_carto(self):
        with patch("france_pv.api", return_value={"totalFeatures": "100", "features": []}):
            with self.assertRaisesRegex(ValueError, "subdivisions"):
                cadastral_carto(self.area)

    def test_cadastral_wfs_failure_uses_carto(self):
        with (patch("france_pv.wfs_features", side_effect=ValueError("down")),
              patch("france_pv.cadastral_carto", return_value=[self.feature]) as fallback):
            self.assertEqual(cadastral(self.area), [self.feature])
            fallback.assert_called_once()

    def test_agricultural_allocation_reports_full_parcel_and_rpg_areas(self):
        agricultural = box(700000, 6600000, 700500, 6601000)
        selected, missing = allocate([self.feature], self.area, Point().buffer(0), agricultural, self.area.centroid, 1000, .4)
        props = selected[0]["properties"]
        self.assertAlmostEqual(props["surface_parcelle_m2"], 1000000, places=1)
        self.assertAlmostEqual(props["surface_agricole_zone_m2"], 500000, places=1)
        self.assertAlmostEqual(props["part_agricole_pct"], 50, places=3)
        self.assertLess(missing, .01)

    def test_official_prices_convert_hectares_and_ignore_subregion_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.xlsx"
            pd.DataFrame([["Prix agricole", None, None, None],
                          ["Occitanie", "2023", "2024", "2025"],
                          ["HÉRAULT", 5000, 6000, 7000],
                          ["SOUS REGION", 50000, 60000, 70000]]).to_excel(path, index=False, header=False)
            self.assertAlmostEqual(parse_price_table(path, "Hérault"), .7)
            with self.assertRaisesRegex(ValueError, "Aucune référence"):
                parse_price_table(path, "Département absent")

    def test_capex_keeps_land_out_of_electrical_contingency(self):
        assumptions = dict(pv_low=650, pv_high=1200, cable_low=80, cable_high=250,
                           fixed_low=20000, fixed_high=100000, transformer_low=30000, transformer_high=80000,
                           detour=1.3, contingency=.25)
        price = price_envelope(.8, .5)
        cost = capex(1000, 100, assumptions, land_area_m2=25000, land_price=price)
        self.assertEqual(cost["land_low"], 10000)
        self.assertAlmostEqual(cost["land_high"], 30000)
        self.assertAlmostEqual(cost["low"], (650000 + 10400 + 20000 + 30000) * 1.25 + 10000)

    def test_dvf_groups_mutations_before_excluding_built_and_bundled_sales(self):
        row = dict(id_mutation="a", nature_mutation="Vente", id_parcelle="p1",
                   numero_disposition="1", code_type_local="", type_local="",
                   surface_reelle_bati="", nombre_lots="0", numero_volume="",
                   code_nature_culture="T", nature_culture="terres",
                   valeur_fonciere="20000", surface_terrain="10000",
                   longitude="3.88", latitude="43.61")
        bundle = dict(row, id_mutation="b")
        mixed = dict(row, id_mutation="c")
        rows = [row, row, bundle, dict(bundle, id_parcelle="p2"), mixed,
                dict(mixed, type_local="Maison", surface_reelle_bati="100")]
        self.assertEqual(list(bare_land_sales(rows)), [("p1","T","terres",10000,2,3.88,43.61)])

    def test_dvf_mean_requires_matching_type_size_and_location(self):
        subject = dict(reference_cadastrale="34172000AB0009", land_category="T",
                       surface_parcelle_m2=10000, part_agricole_pct=90)
        sales = [(str(i),"T","terres",10000,price,3.88,43.61) for i,price in enumerate((1,2,3))]
        sales += [("wrong-type","P","prés",10000,50,3.88,43.61),
                  ("small","T","terres",1000,50,3.88,43.61),
                  ("far","T","terres",10000,50,4,43.61)]
        area = metric(Point(3.88,43.61)).buffer(1000)
        with patch("land_prices.commune_sales", side_effect=[sales, [], []]):
            price = local_land_prices([subject], area, lambda _:None)[subject["reference_cadastrale"]]
        self.assertEqual(price["eur_m2"], 2)
        self.assertEqual(price["count"], 3)

    def test_missing_network_capacity_is_not_invented(self):
        asset = {"id": "Enedis/test", "kind": "transformer", "voltage": "inconnue", "voltage_class": "HTA/BT", "geometry": self.area.centroid}
        row = asset_table([asset], self.area.centroid).iloc[0]
        self.assertEqual(row["Puissance assignée (donnée source)"], "non publiée")
        self.assertEqual(row["Capacité d'injection disponible"], "non publiée")

    def test_agricultural_filter_excludes_mixed_parcels_below_threshold(self):
        fields = box(700000, 6600000, 700500, 6601000)
        self.assertEqual(agricultural_cadastre([self.feature], fields, self.area, .8), [])
        selected = agricultural_cadastre([self.feature], fields, self.area, .4)
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(selected[0]["properties"]["part_agricole_pct"], 50, places=3)


if __name__ == "__main__":
    unittest.main()

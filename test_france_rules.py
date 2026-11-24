import unittest
from unittest.mock import patch
from shapely.geometry import Point,box,mapping
from pydantic import ValidationError
import france_pv as core
from backend.models import ProjectInput,NetworkInput
from backend.jobs import france_snapshot
from france_rules import apply_french_rules,legal_perimeter
from countries import COUNTRIES,search_area
from regulatory import validated_features


class FrenchRulesTests(unittest.TestCase):
    def setUp(self):
        self.point=core.metric(Point(3.88,43.61));self.area=self.point.buffer(100)
        self.regulation=dict(exclusions=Point().buffer(0),checks=[],features=[])

    def legal_data(self):
        return dict(type='FeatureCollection',features=[dict(type='Feature',geometry=mapping(core.geographic(self.area)),properties=dict(legal_basis='document_cadre_L111_29',source_url='https://www.herault.gouv.fr/document-cadre',reference='Référence de test',approval_date='2026-01-01'))])

    def test_france_only_and_thousand_km_are_validated(self):
        self.assertEqual(list(COUNTRIES),['France'])
        self.assertEqual(ProjectInput(radius=1000).radius,1000)
        for payload in [dict(radius=1000.1),dict(country='ES'),dict(lat=40),dict(lon=11)]:
            with self.assertRaises(ValidationError):ProjectInput(**payload)
        with self.assertRaises(ValidationError):NetworkInput(country='ES',lat=43,lon=3)

    def test_large_radius_is_passed_to_french_clipping_not_to_network_tiles(self):
        with patch('france_pv.gps_area',return_value=self.area) as clip:
            self.assertEqual(search_area(43.61,3.88,1000),self.area)
        clip.assert_called_once_with(43.61,3.88,1000)

    def test_actual_national_circle_is_clipped_to_french_geometry(self):
        border=box(2,43,5,45)
        with patch('france_pv.api',side_effect=[[dict(code='34000')],dict(features=[dict(geometry=mapping(border))])]):
            area=core.gps_area(43.61,3.88,1000)
        self.assertLess(area.difference(core.metric(border)).area,1)

    def test_ground_project_has_no_invented_legal_perimeter(self):
        result=apply_french_rules(ProjectInput(siting_mode='ground'),self.area,self.regulation)
        self.assertLess(self.area.difference(result['exclusions']).area,.01)
        self.assertEqual(result['checks'][-1]['status'],'missing')

    def test_ground_project_clips_to_documented_perimeter(self):
        data=self.legal_data()
        result=apply_french_rules(ProjectInput(siting_mode='ground',legal_areas_geojson=data),self.area,self.regulation)
        self.assertGreater(result['legal_coverage'].area,self.area.area*.99)
        self.assertEqual(result['legal_evidence'][0]['legal_basis'],'document_cadre_L111_29')
        self.assertEqual(result['checks'][-1]['status'],'declared')

    def test_legal_import_requires_traceable_official_reference(self):
        data=self.legal_data();data['features'][0]['properties']['source_url']='https://www.herault.gouv.fr.attacker.test/file'
        with self.assertRaises(ValidationError):ProjectInput(legal_areas_geojson=data)
        with self.assertRaises(ValueError):legal_perimeter(dict(type='FeatureCollection',features=[{}]),self.area)

    def test_legal_coverage_rule_is_not_applied_as_a_universal_forty_percent_limit(self):
        blocked=apply_french_rules(ProjectInput(surface=60000,legal_coverage_percent=41),self.area,self.regulation)
        self.assertEqual(blocked['checks'][-1]['status'],'blocked')
        self.assertLess(self.area.difference(blocked['exclusions']).area,.01)
        small=apply_french_rules(ProjectInput(surface=10000,legal_coverage_percent=41),self.area,self.regulation)
        self.assertNotEqual(small['checks'][-1]['status'],'blocked')
        unknown=apply_french_rules(ProjectInput(surface=60000,coverage=.7),self.area,self.regulation)
        self.assertEqual(unknown['checks'][-1]['status'],'unknown')

    def test_natural_and_servitude_policy_is_effective_and_explicit(self):
        feature=dict(type='Feature',geometry=mapping(core.geographic(self.area)),properties=dict(constraint='Zone naturelle GPU'))
        result=apply_french_rules(ProjectInput(),self.area,{**self.regulation,'features':[feature]})
        self.assertLess(self.area.difference(result['exclusions']).area,.01)
        relaxed=apply_french_rules(ProjectInput(avoid_natural_zones=False),self.area,{**self.regulation,'features':[feature]})
        self.assertTrue(relaxed['exclusions'].is_empty)
        self.assertEqual(self.regulation['checks'],[])

    def test_foreign_history_is_not_exposed_or_deleted(self):
        foreign=dict(config=dict(country='ES'),summary=dict(terrain_country='ES'))
        self.assertIsNone(france_snapshot(foreign))
        french=dict(config=dict(country='legacy'),summary=dict(terrain_country='France'))
        self.assertEqual(france_snapshot(french)['config']['country'],'France')

    def test_invalid_official_surface_is_repaired_and_flagged_without_mutating_source(self):
        from shapely.geometry import Polygon,shape
        invalid=Polygon([(0,0),(1,1),(1,0),(0,1),(0,0)])
        data=dict(type='FeatureCollection',features=[dict(type='Feature',geometry=mapping(invalid),properties=dict(reference='official'))])
        rows=validated_features(data,'GPU')
        self.assertTrue(shape(rows[0]['geometry']).is_valid)
        self.assertGreater(shape(rows[0]['geometry']).area,0)
        self.assertTrue(rows[0]['properties']['geometry_repaired'])
        self.assertNotIn('geometry_repaired',data['features'][0]['properties'])


if __name__=='__main__':unittest.main()

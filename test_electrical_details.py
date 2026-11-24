import unittest
from unittest.mock import patch
from electrical_data import exact_osm_details,current_amperes
from backend.main import create_app
from fastapi.testclient import TestClient


class ElectricalDetailsTests(unittest.TestCase):
    def test_exact_cable_tags_and_units_are_preserved(self):
        data=dict(elements=[dict(type='way',id=42,version=3,timestamp='2026-01-01T00:00:00Z',tags={'power':'cable','voltage':'20000','max_current':'0.63 kA','cables':'3','circuits':'1','cable:material':'aluminium','cable:cross_section':'240 mm²'})])
        with patch('france_pv.api',return_value=data) as fetch:
            result=exact_osm_details('way',42)
        self.assertEqual(result['current_a'],630)
        self.assertEqual(result['voltage_label'],'20 kV')
        self.assertEqual(result['cross_section'],'240 mm²')
        self.assertEqual(result['cables'],'3')
        self.assertEqual(result['source_version'],3)
        self.assertEqual(fetch.call_args.args[0],'https://api.openstreetmap.org/api/0.6/way/42.json')

    def test_no_rating_is_invented_and_mismatched_ids_are_rejected(self):
        with patch('france_pv.api',return_value=dict(elements=[dict(type='way',id=42,tags={'power':'cable','voltage':'20000'})])):
            result=exact_osm_details('way',42)
            self.assertNotIn('current_a',result)
            self.assertNotIn('cross_section',result)
            with self.assertRaises(ValueError):exact_osm_details('way',43)

    def test_invalid_types_and_service_failure_have_explicit_http_status(self):
        with TestClient(create_app()) as client:
            self.assertEqual(client.get('/api/electrical/anything/42').status_code,422)
            self.assertEqual(client.get('/api/electrical/way/-1').status_code,422)
            with patch('electrical_data.exact_osm_details',side_effect=ValueError('source down')):
                self.assertEqual(client.get('/api/electrical/way/42').status_code,503)

    def test_current_parser_does_not_choose_a_rating_from_a_compound_value(self):
        self.assertEqual(current_amperes('0,4 kA'),400)
        self.assertIsNone(current_amperes('400;600'))
        self.assertIsNone(current_amperes('inconnue'))

    def test_vector_tile_preserves_published_line_current_and_conductors(self):
        import mapbox_vector_tile
        from openinfra_network import decode_tile
        feature=dict(id=42,geometry=dict(type='LineString',coordinates=[[1000,1000],[2000,2000]]),properties=dict(type='cable',voltage=20,max_current='0.63 kA',cables='3',circuits='1'))
        tile=mapbox_vector_tile.encode(dict(name='power_line',features=[feature]),default_options=dict(y_coord_down=True))
        asset=decode_tile(tile,14,8470,5838)[0]
        self.assertEqual(asset['kind'],'cable')
        self.assertEqual(asset['current_a'],630)
        self.assertEqual(asset['cables'],'3')
        self.assertEqual(asset['voltage_values_v'],[20000])


if __name__=='__main__':unittest.main()

import unittest
from unittest.mock import patch
from shapely.geometry import Point
from pydantic import ValidationError
import france_pv as core
from backend.models import ProjectInput,NetworkInput
from backend.analysis import analyze
from connection_preferences import apply_voltage_preferences,capex_for_connection
from shared_grid import shared_connection_plan
from test_backend import AnalysisFixture


class ConnectionPreferencesTests(unittest.TestCase):
    def asset(self,identifier,voltage,x=10,**kwargs):
        return dict(id=identifier,kind='line',geometry=Point(x,0),voltage=voltage,**kwargs)

    def test_empty_unsupported_and_nonfinite_selections_are_rejected(self):
        for values in ([],[21],[float('nan')]):
            with self.assertRaises(ValidationError):ProjectInput(connection_voltages_kv=values)
            with self.assertRaises(ValidationError):NetworkInput(lat=43.6,lon=3.8,connection_voltages_kv=values)
        self.assertEqual(ProjectInput(connection_voltages_kv=[20,15,20]).connection_voltages_kv,[15,20])

    def test_known_requested_line_wins_over_closer_incompatible_or_unknown_assets(self):
        assets=[self.asset('unknown','inconnue',1),self.asset('bt','400',2),self.asset('hta','20000',100)]
        filtered=apply_voltage_preferences(assets,[20])
        self.assertEqual(len(filtered),3)
        self.assertEqual(core.connection(Point(0,0),filtered)['id'],'hta')
        self.assertNotIn('voltage_selection_compatible',assets[0])

    def test_transformer_windings_and_multivoltage_lines_are_matched(self):
        assets=[dict(id='t',kind='transformer',geometry=Point(10,0),voltage='inconnue',primary_voltage_v=20000,secondary_voltage_v=400),self.asset('multi','63000;20000')]
        filtered=apply_voltage_preferences(assets,[.4])
        self.assertEqual(core.connection(Point(0,0),filtered)['id'],'t')
        self.assertEqual(filtered[0]['voltage_selection_matched_v'],[400])
        self.assertFalse(filtered[1]['voltage_selection_compatible'])

    def test_voltage_match_does_not_unlock_zero_capacity_or_unavailable_equipment(self):
        assets=[self.asset('zero','20000',available_generation_mw=0),self.asset('closed','20000',planning_eligible=False)]
        self.assertIsNone(core.connection(Point(0,0),apply_voltage_preferences(assets,[20])))

    def test_explicit_htb_is_explorable_but_distribution_total_is_not_used(self):
        assets=apply_voltage_preferences([self.asset('htb','63000')],[63])
        link=core.connection(Point(0,0),assets)
        self.assertIsNotNone(link)
        cost=capex_for_connection(dict(low=100,high=200,pv_low=80),link)
        self.assertIsNone(cost['high'])
        self.assertTrue(cost['grid_quote_required'])
        self.assertEqual(cost['pv_low'],80)

    def test_single_external_connection_respects_voltage_for_all_fields(self):
        assets=apply_voltage_preferences([self.asset('bt','400',1),self.asset('selected','20000',100)],[20])
        parcels=[dict(parcelle='a',geometry=Point(0,0),pv_m2=100),dict(parcelle='b',geometry=Point(5,0),pv_m2=100)]
        assumptions=dict(cable_low=80,cable_high=250,detour=1.3,fixed_low=1,fixed_high=2,contingency=.25)
        plan=shared_connection_plan(parcels,assets,assumptions)
        self.assertEqual(plan['connections'],1)
        self.assertEqual(plan['target']['id'],'selected')

    def test_htb_export_and_total_do_not_reuse_distribution_prices(self):
        from shared_grid import shared_capex
        assets=apply_voltage_preferences([self.asset('htb','63000')],[63])
        assumptions=dict(pv_low=650,pv_high=1200,cable_low=80,cable_high=250,detour=1.3,fixed_low=1,fixed_high=2,contingency=.25)
        plan=shared_connection_plan([dict(parcelle='a',geometry=Point(0,0),pv_m2=100)],assets,assumptions)
        props=plan['features'][0]['properties']
        self.assertIsNone(props['liaison_basse_eur'])
        self.assertIsNone(props['raccordement_haut_eur'])
        self.assertEqual(props['tensions_selectionnees_v'],[63000])
        self.assertIsNone(shared_capex(plan,21,assumptions,[])['high'])

    def test_complete_analysis_does_not_fall_back_to_an_unselected_voltage(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory,patch('france_pv.CACHE',Path(directory)):
            with AnalysisFixture() as fixture:
                result=analyze(fixture.config.model_copy(update=dict(connection_voltages_kv=[63])))
        self.assertIsNone(result['collector']['target'])
        self.assertTrue(result['scenarios'][1]['unavailable'])
        self.assertEqual(len(result['assets']),1)
        self.assertFalse(result['assets'][0]['voltage_selection_compatible'])


if __name__=='__main__':unittest.main()

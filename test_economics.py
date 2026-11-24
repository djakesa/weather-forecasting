"""Analytical finance checks and persistence/API tests, with no provider calls."""
import copy
import csv
import io
import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.economics import EconomicsInput,calculate_economics
from backend.jobs import JobManager
from backend.main import create_app


def example():
    scenario=dict(name='Test',energy=1000,unavailable=False,land_included=True,cost=dict(low=1000,high=1000))
    return dict(id='b'*32,kind='analysis',config=dict(country='France',density=100),summary=dict(kwp=1,surface=10,placed_m2=10),scenarios=[copy.deepcopy(scenario) for _ in range(3)])


def parameters(**updates):
    return EconomicsInput(years=10,sale_price_eur_mwh=200,opex_eur_kwp_year=0,discount_percent=0,degradation_percent=0,replacement_eur_kwp=0,**updates)


class EconomicsTests(unittest.TestCase):
    def test_partially_priced_land_is_reported_with_distinct_parcel_counts(self):
        data=example()
        data['scenarios'][2].update(land_included=False,cost=dict(low=1100,high=1300,land_low=100,land_high=300))
        data['parcels']=[
            dict(reference_cadastrale='A',achat_bas_eur=100),
            dict(reference_cadastrale='A',achat_bas_eur=0),
            dict(reference_cadastrale='B',achat_bas_eur=None),
        ]
        result=calculate_economics(data,parameters())
        self.assertEqual(result['acquisition'],dict(status='partial',low_eur=100,high_eur=300,priced_parcels=1,total_parcels=2))
        self.assertEqual(result['central']['capex_eur'],1200)
        manual=calculate_economics(data,parameters(capex_override_eur=2000))
        self.assertEqual(manual['acquisition'],{'status':'manual'})

    def test_known_cashflows_payback_and_lcoe(self):
        result=calculate_economics(example(),parameters());value=result['central']
        self.assertEqual(value['payback_years'],5)
        self.assertEqual(value['discounted_payback_years'],5)
        self.assertEqual(value['lcoe_eur_mwh'],100)
        self.assertEqual(value['npv_eur'],1000)
        self.assertEqual(value['roi_percent'],100)
        self.assertEqual(value['rows'][0]['net_cashflow_eur'],-1000)
        self.assertEqual(value['rows'][1]['revenue_eur'],200)

    def test_discounted_cost_and_energy_follow_analytical_formula(self):
        config=parameters().model_copy(update=dict(discount_percent=10))
        value=calculate_economics(example(),config)['central']
        annuity=sum(1/1.1**year for year in range(1,11))
        self.assertAlmostEqual(value['npv_eur'],-1000+200*annuity)
        self.assertAlmostEqual(value['lcoe_eur_mwh'],1000/annuity)
        self.assertGreater(value['discounted_payback_years'],value['payback_years'])

    def test_replacement_and_end_cost_are_booked_in_the_correct_years(self):
        config=parameters().model_copy(update=dict(replacement_year=5,replacement_eur_kwp=200,decommissioning_eur=100))
        value=calculate_economics(example(),config)['central']
        self.assertEqual(value['rows'][5]['replacement_eur'],200)
        self.assertEqual(value['rows'][10]['decommissioning_eur'],100)
        self.assertEqual(value['net_gain_eur'],700)
        self.assertEqual(value['payback_years'],6)
        self.assertEqual(value['lcoe_eur_mwh'],130)

    def test_zero_revenue_returns_no_payback_without_infinity(self):
        value=calculate_economics(example(),parameters().model_copy(update=dict(sale_price_eur_mwh=0)))
        self.assertIsNone(value['central']['payback_years'])
        self.assertIsNone(value['central']['discounted_payback_years'])
        self.assertEqual(value['central']['npv_eur'],-1000)
        json.dumps(value,allow_nan=False)

    def test_degradation_starts_after_first_year(self):
        value=calculate_economics(example(),parameters().model_copy(update=dict(degradation_percent=1)))['central']
        self.assertEqual(value['rows'][1]['energy_kwh'],1000)
        self.assertEqual(value['rows'][2]['energy_kwh'],990)
        self.assertAlmostEqual(value['rows'][10]['energy_kwh'],1000*.99**9)

    def test_capex_range_and_manual_override(self):
        data=example();data['scenarios'][2]['cost']['high']=2000
        value=calculate_economics(data,parameters())
        self.assertEqual(value['central']['lcoe_eur_mwh'],150)
        self.assertEqual(value['optimistic']['lcoe_eur_mwh'],100)
        self.assertEqual(value['pessimistic']['lcoe_eur_mwh'],200)
        data['scenarios'][2]['cost']=dict(low=None,high=None)
        data['collector']=dict(connections=0)
        self.assertEqual(calculate_economics(data,parameters())['status'],'incomplete')
        overridden=calculate_economics(data,parameters(capex_override_eur=1200))
        self.assertEqual(overridden['central']['capex_eur'],1200)
        self.assertEqual(overridden['capex_source'],'manual')
        self.assertTrue(any('Raccordement non identifié' in note for note in overridden['notes']))

    def test_partial_installation_uses_only_installed_capacity(self):
        data=example();data['summary']['placed_m2']=5;data['scenarios'][2]['energy']=500
        config=parameters().model_copy(update=dict(opex_eur_kwp_year=10))
        value=calculate_economics(data,config)
        self.assertEqual(value['installed_kwp'],.5)
        self.assertEqual(value['central']['rows'][1]['opex_eur'],5)
        self.assertTrue(any('partielle' in note for note in value['notes']))

    def test_missing_production_cannot_be_fixed_with_a_capex_override(self):
        data=example();data['scenarios'][2]['unavailable']=True
        self.assertEqual(calculate_economics(data,parameters(capex_override_eur=1000))['status'],'unavailable')
        data=example();data['scenarios'][2]['energy']=0
        self.assertEqual(calculate_economics(data,parameters())['status'],'unavailable')

    def test_payback_reversal_is_reported(self):
        value=calculate_economics(example(),parameters().model_copy(update=dict(decommissioning_eur=2500)))['central']
        self.assertEqual(value['payback_years'],5)
        self.assertTrue(value['payback_reversed'])
        self.assertLess(value['net_gain_eur'],0)

    def test_discounted_recovery_can_be_reversed_without_reversing_simple_recovery(self):
        config=parameters().model_copy(update=dict(discount_percent=10,decommissioning_eur=700))
        value=calculate_economics(example(),config)['central']
        self.assertFalse(value['payback_reversed'])
        self.assertTrue(value['discounted_payback_reversed'])
        self.assertLess(value['npv_eur'],0)
        self.assertGreater(value['net_gain_eur'],0)


class EconomicsAPITests(unittest.TestCase):
    def setUp(self):
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.manager=JobManager(Path(folder.name));self.addCleanup(self.manager.executor.shutdown,wait=True)
        directory=self.manager.directory/('b'*32);directory.mkdir()
        (directory/'result.json').write_text(json.dumps(example()),encoding='utf-8')
        self.client=TestClient(create_app(self.manager));self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.url='/api/projects/'+('b'*32)

    def test_recalculation_is_saved_exported_and_never_calls_weather(self):
        with patch('france_pv.requests.get') as provider:
            response=self.client.post(self.url+'/economics',json=parameters().model_dump())
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['central']['payback_years'],5)
            again=self.client.get(self.url+'/economics').json()
            self.assertEqual(again['assumptions']['sale_price_eur_mwh'],200)
            exported=self.client.get(self.url+'/export/economics.csv')
            self.assertEqual(exported.status_code,200)
            rows=list(csv.reader(io.StringIO(exported.text),delimiter=';'))
            self.assertEqual(len(rows),12)
            self.assertTrue(any('Cumul' in cell for cell in rows[0]))
            provider.assert_not_called()
        self.assertTrue((self.manager.directory/('b'*32)/'economics.json').exists())

    def test_invalid_assumptions_do_not_overwrite_a_saved_simulation(self):
        self.client.post(self.url+'/economics',json=parameters().model_dump())
        self.assertEqual(self.client.post(self.url+'/economics',json=dict(discount_percent=-1)).status_code,422)
        self.assertEqual(self.client.get(self.url+'/economics').json()['assumptions']['discount_percent'],0)
        self.assertEqual(self.client.get('/api/projects/'+('a'*32)+'/economics').status_code,404)


if __name__=='__main__':unittest.main()

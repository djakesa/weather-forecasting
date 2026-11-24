import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET
import pandas as pd
from shapely.geometry import Point,mapping
from shapely.ops import unary_union
import france_pv as core
from backend.models import ProjectInput
from backend.siting import candidate_centers,evaluate_sites
from regulatory import regulatory_context,validated_features
from countries import electrical_network


class SitingTests(unittest.TestCase):
    def setUp(self):
        self.point=core.metric(Point(3.88,43.61))
        self.config=ProjectInput(country='France',parcel_radius=.2)
        self.empty=Point().buffer(0)
        grid=patch('backend.siting.grid_context',return_value=dict(status='checked',features=[],coverage_share=1,exclusions=self.empty))
        grid.start();self.addCleanup(grid.stop)
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        patcher=patch('france_pv.CACHE',Path(temporary.name));patcher.start();self.addCleanup(patcher.stop)

    def test_candidate_budget_and_solar_loss_are_respected(self):
        samples=pd.DataFrame([dict(x=i*3000,y=0,**{'yield':value}) for i,value in enumerate([100,99,98,97,10])])
        centers=candidate_centers(Point(0,0),samples,3,.2)
        self.assertEqual(len(centers),3)
        self.assertNotIn(Point(12000,0),centers)

    def test_sparse_weather_still_explores_neighbouring_land_with_labelled_proxies(self):
        samples=pd.DataFrame([dict(x=0,y=0,**{'yield':100})])
        boundary=Point(0,0).buffer(10000)
        centers=candidate_centers(Point(0,0),samples,3,1,boundary=boundary,proxy_distance=2500)
        self.assertEqual(len(centers),3)
        self.assertTrue(all(boundary.covers(p) and p.distance(Point(0,0))<=2500 for p in centers))

    def test_no_proxy_is_created_outside_the_selection_or_weather_distance_limit(self):
        samples=pd.DataFrame([dict(x=0,y=0,**{'yield':100})])
        self.assertEqual(len(candidate_centers(Point(0,0),samples,3,1,boundary=Point(0,0).buffer(1000),proxy_distance=2500)),1)
        self.assertEqual(len(candidate_centers(Point(0,0),samples,3,2,boundary=Point(0,0).buffer(10000),proxy_distance=2500)),1)

    def test_second_sector_survives_cadastre_failure_with_gpu_and_rpg_indicators(self):
        p=self.point;q=Point(p.x+4000,p.y)
        boundary=unary_union([p.buffer(300),q.buffer(300)])
        samples=pd.DataFrame([dict(x=p.x,y=p.y,**{'yield':1500}),dict(x=q.x,y=q.y,**{'yield':1490})])
        asset=dict(id='line',kind='line',geometry=Point(q.x+250,q.y),voltage='20000')
        parcel=dict(type='Feature',geometry=mapping(core.geographic(q.buffer(200))),properties=dict(idu='second'))
        zone=dict(type='Feature',geometry=parcel['geometry'],properties=dict(layer='zone-urba',typezone='N'))
        regulation=dict(status='partial',checks=[],features=[zone])
        with patch('backend.siting.regulatory_context',return_value=regulation):
            def parcels(area,*args,**kwargs):
                if area.distance(p)<1:raise ValueError('source down')
                return [parcel]
            result=evaluate_sites(self.config,boundary,p,samples,[],lambda _:None,parcels,lambda *a,**k:(self.empty,self.empty),lambda *a,**k:([asset],self.empty,self.empty),lambda *a:'France')
        self.assertEqual(result['center'],q)
        self.assertTrue(result['computed'])
        self.assertEqual(len(result['reports']),2)
        self.assertIn('Cadastre indisponible',result['reports'][0]['issues'][0])
        self.assertEqual(result['features'][0]['geometry'],parcel['geometry'])
        self.assertEqual(result['features'][0]['properties']['idu'],'second')
        self.assertEqual(result['features'][0]['properties']['gpu_intersections'][0]['typezone'],'N')
        self.assertTrue(result['footprints'])
        self.assertEqual(result['footprints'][0]['properties']['part_agricole_pct'],0)

    def test_partial_observation_does_not_classify_unobserved_parcel_as_non_agricultural(self):
        parcel=dict(type='Feature',geometry=mapping(core.geographic(self.point.buffer(1000))),properties=dict(idu='large'))
        observed=self.point.buffer(100)
        selected=core.agricultural_cadastre([parcel],observed,observed,.8)
        self.assertEqual(len(selected),1)
        self.assertAlmostEqual(selected[0]['properties']['part_agricole_pct'],100,places=4)

    def test_empty_initial_sector_triggers_bounded_search_at_lower_solar_yield(self):
        p=self.point;q=Point(p.x+6000,p.y)
        boundary=unary_union([p.buffer(250),q.buffer(250)])
        samples=pd.DataFrame([dict(x=p.x,y=p.y,**{'yield':1500}),dict(x=q.x,y=q.y,**{'yield':1200})])
        parcels=[dict(type='Feature',geometry=mapping(core.geographic(point.buffer(200))),properties=dict(idu=name)) for name,point in [('p',p),('q',q)]]
        regulation=dict(status='checked',checks=[],features=[],exclusions=self.empty)
        def terrain(area,*args,**kwargs):return (area if area.distance(p)<1 else self.empty,boundary)
        with patch('backend.siting.regulatory_context',return_value=regulation):
            result=evaluate_sites(self.config.model_copy(update=dict(max_sectors=2)),boundary,p,samples,[],lambda _:None,lambda *a,**k:parcels,terrain,lambda *a,**k:([],self.empty,self.empty),lambda *a:'France')
        self.assertEqual(result['center'],q)
        self.assertTrue(result['search_expanded'])
        self.assertLessEqual(len(result['reports']),4)
        self.assertAlmostEqual(result['reports'][-1]['solar_loss_percent'],20)
        self.assertGreater(result['reports'][0]['land_diagnostics'][0]['removed_m2'],1000)
        self.assertGreater(sum(f['properties']['pv_m2'] for f in result['footprints']),9900)

    def test_saturated_sector_is_avoided_and_target_stays_filtered_in_final_plan(self):
        p=self.point;q=Point(p.x+4000,p.y)
        boundary=unary_union([p.buffer(300),q.buffer(300)])
        samples=pd.DataFrame([dict(x=p.x,y=p.y,**{'yield':1500}),dict(x=q.x,y=q.y,**{'yield':1490})])
        assets=[dict(id=name,kind='transformer',voltage='20000',geometry=Point(point.x+250,point.y)) for name,point in [('sat',p),('ok',q)]]
        def grid(area,*args):
            sat=area.distance(p)<1;geom=p.buffer(1500) if sat else q.buffer(1500)
            return dict(status='checked',coverage_share=1,features=[dict(type='Feature',geometry=mapping(core.geographic(geom)),properties=dict(id='zone',status='SATUREE' if sat else 'FAVORABLE'))],exclusions=geom if sat else self.empty)
        parcels=[dict(type='Feature',geometry=mapping(core.geographic(point.buffer(200))),properties=dict(idu=name)) for name,point in [('p',p),('q',q)]]
        with patch('backend.siting.grid_context',side_effect=grid),patch('backend.siting.regulatory_context',return_value=dict(status='checked',checks=[],features=[],exclusions=self.empty)):
            result=evaluate_sites(self.config,boundary,p,samples,assets,lambda _:None,lambda *a,**k:parcels,lambda *a,**k:(self.empty,boundary),lambda *a,**k:([],self.empty,self.empty),lambda *a:'France')
        self.assertEqual(result['center'],q)
        self.assertGreater(sum(f['properties']['pv_m2'] for f in result['footprints']),9900)
        self.assertEqual(core.connection(p,result['assets'])['id'],'ok')

    def test_full_land_without_local_grid_continues_to_lower_solar_sector(self):
        p=self.point;q=Point(p.x+6000,p.y)
        boundary=unary_union([p.buffer(250),q.buffer(250)])
        samples=pd.DataFrame([dict(x=p.x,y=p.y,**{'yield':1500}),dict(x=q.x,y=q.y,**{'yield':1200})])
        parcels=[dict(type='Feature',geometry=mapping(core.geographic(point.buffer(200))),properties=dict(idu=name)) for name,point in [('p',p),('q',q)]]
        asset=dict(id='lower-site-line',kind='line',voltage='20000',geometry=Point(q.x+300,q.y))
        regulation=dict(status='checked',checks=[],features=[],exclusions=self.empty)
        with patch('backend.siting.regulatory_context',return_value=regulation),patch('france_pv.solar') as weather:
            result=evaluate_sites(self.config.model_copy(update=dict(max_sectors=2)),boundary,p,samples,[],lambda _:None,lambda *a,**k:parcels,lambda *a,**k:(self.empty,boundary),lambda *a,**k:([asset],self.empty,self.empty),lambda *a:'France')
        self.assertEqual(result['center'],q)
        self.assertEqual(result['reports'][0]['decision'],'no_connection')
        self.assertEqual(result['reports'][1]['decision'],'accepted')
        self.assertTrue(result['search_expanded'])
        self.assertEqual(result['connection_assets'][0]['id'],'lower-site-line')
        weather.assert_not_called()

    def test_best_feasible_solar_site_is_kept_even_if_next_site_has_cheaper_link(self):
        p=self.point;q=Point(p.x+6000,p.y)
        boundary=unary_union([p.buffer(250),q.buffer(250)])
        samples=pd.DataFrame([dict(x=p.x,y=p.y,**{'yield':1500}),dict(x=q.x,y=q.y,**{'yield':1490})])
        parcels=[dict(type='Feature',geometry=mapping(core.geographic(point.buffer(200))),properties=dict(idu=name)) for name,point in [('p',p),('q',q)]]
        assets=[dict(id='solar-line',kind='line',voltage='20000',geometry=Point(p.x+800,p.y)),dict(id='cheaper-line',kind='line',voltage='20000',geometry=Point(q.x+30,q.y))]
        regulation=dict(status='checked',checks=[],features=[],exclusions=self.empty)
        from unittest.mock import Mock
        network=Mock(return_value=([],self.empty,self.empty))
        with patch('backend.siting.regulatory_context',return_value=regulation):
            result=evaluate_sites(self.config,boundary,p,samples,assets,lambda _:None,lambda *a,**k:parcels,lambda *a,**k:(self.empty,boundary),network,lambda *a:'France')
        self.assertEqual(result['center'],p)
        self.assertEqual(len(result['reports']),1)
        self.assertEqual(result['reports'][0]['decision'],'accepted')
        self.assertEqual([a['id'] for a in result['connection_assets']],['solar-line'])
        network.assert_not_called()

    def test_official_network_fallback_preserves_separate_source_ids(self):
        official=dict(id='Enedis/one',kind='transformer',geometry=self.point,voltage='inconnue')
        with patch('openinfra_network.network_assets',return_value=[]),patch('france_pv.enedis_assets',return_value=[official]),patch('france_pv.ign_electrical_assets',return_value=[]):
            assets,_,_=electrical_network(self.point.buffer(100),'France')
        self.assertEqual(assets[0]['id'],'Enedis/one')
        self.assertEqual(assets[0]['voltage'],'inconnue')

    def test_failed_gpu_layers_are_reported_as_unavailable(self):
        with patch('france_pv.api',side_effect=ValueError('service unavailable')):
            result=regulatory_context(self.point.buffer(100),'France')
        self.assertEqual(result['status'],'partial')
        self.assertTrue(all(c['status']=='unavailable' for c in result['checks']))
        self.assertEqual(result['features'],[])

    def test_incomplete_protection_response_is_never_accepted(self):
        with self.assertRaisesRegex(ValueError,'tronquée'):
            validated_features(dict(type='FeatureCollection',features=[],exceededTransferLimit=True),'EEA')

    def test_xml_cooldown_still_allows_cached_responses(self):
        import osm_sources as es
        import hashlib,json,time
        url='https://cadastre.example.test/INSPIRE/wfs'
        cache=core.CACHE/('xml_'+hashlib.sha256(json.dumps([url,None],sort_keys=True).encode()).hexdigest()+'.xml')
        cache.write_text('<FeatureCollection/>')
        cooldown=core.CACHE/('xml_cooldown_'+hashlib.sha256(b'cadastre.example.test').hexdigest()+'.json')
        cooldown.write_text(json.dumps(dict(until=time.time()+60)))
        with patch('osm_sources.requests.get') as request:
            self.assertEqual(es.xml_response(url).tag,'FeatureCollection')
            with self.assertRaisesRegex(ValueError,'temporairement'):
                es.xml_response(url,params=dict(other=1))
            request.assert_not_called()


if __name__=='__main__':unittest.main()

"""Offline checks of guided pages, help, explicit point selection and loading steps."""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from playwright.sync_api import sync_playwright,TimeoutError as BrowserTimeout

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_backend import AnalysisFixture
from backend.analysis import analyze
from backend.serialization import serialize_result
from backend.economics import EconomicsInput,calculate_economics


def main():
    base=os.environ.get('ATLAS_URL','http://127.0.0.1:8765')
    output=ROOT/'data'/'ui-checks';output.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory() as directory,patch('france_pv.CACHE',Path(directory)/'cache'),AnalysisFixture() as fixture:
        config=fixture.config.model_copy(update=dict(surface=12000,name='Projet démonstration'))
        payload=serialize_result(analyze(config),config,'e'*32,Path(directory)/'result')
        png=(Path(directory)/'result'/'irradiance.png').read_bytes()
    payload['grid_constraints']={'features':[{**payload['cadastre']['features'][0],
                                             'properties':{'status':'EN_TENSION','label':'En tension'}}]}
    payload['cadastre']['features'].append({**payload['cadastre']['features'][0],
                                            'properties':{'idu':'unused-parcel'}})
    errors=[];submitted=[];polls=[0];financial=[EconomicsInput()];financial_posts=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport=dict(width=1440,height=1000))
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('**/api/projects/'+('e'*32),lambda route:route.fulfill(content_type='application/json',body=json.dumps(payload)))
        page.route('**/api/projects/'+('e'*32)+'/irradiance.png',lambda route:route.fulfill(content_type='image/png',body=png))
        def economics(route):
            if route.request.method=='POST':
                financial[0]=EconomicsInput.model_validate(route.request.post_data_json)
                financial_posts.append(route.request.post_data_json)
            route.fulfill(content_type='application/json',body=json.dumps(calculate_economics(payload,financial[0])))
        page.route('**/api/projects/'+('e'*32)+'/economics',economics)
        page.route('**/api/electrical/**',lambda route:route.fulfill(content_type='application/json',body=json.dumps(dict(voltage_label='20 kV',current_a=630,cables=3,circuits=1,conductor_material='aluminium',cross_section='240 mm²',source_timestamp='2026-01-01T00:00:00Z'))))
        def start(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(content_type='application/json',body=json.dumps(dict(id='a'*32)))
        def status(route):
            polls[0]+=1;index=min(polls[0]-1,3)
            steps=[dict(key=key,state='done' if i<index else 'running' if i==index else 'pending') for i,key in enumerate(['solar','sites','render'])]
            data=dict(status='done' if index==3 else 'running',phase=['Météo','Parcelles','Carte','Terminé'][index],message='Étape de test',logs=[],progress=dict(completed_steps=index,total_steps=3,steps=steps),result_url='/api/projects/'+('e'*32))
            route.fulfill(content_type='application/json',body=json.dumps(data))
        page.route('**/api/analyses',start)
        page.route('**/api/jobs/'+('a'*32),status)
        page.goto(base,wait_until='networkidle')
        page.locator('#api-status').filter(has_text='connecté').wait_for(state='attached')
        assert page.locator('body').get_attribute('data-view')=='projet'
        assert not page.locator('#map').is_visible()
        assert page.locator('#surface').is_visible()
        assert page.locator('.field-info').count()>20
        page.get_by_role('button',name='Aide : surface',exact=True).click()
        help_id=page.get_by_role('button',name='Aide : surface',exact=True).get_attribute('aria-controls')
        assert page.locator('#'+help_id).is_visible()
        page.keyboard.press('Escape')
        territory=page.locator('.setup-group > summary').filter(has_text='Données territoriales')
        territory.click()
        assert page.locator('#cadastre-file').is_visible()
        assert page.locator('#rpg-file').is_visible()
        assert page.locator('[name="rpg_year"]').input_value()=='2024'
        sample={'type':'FeatureCollection','features':payload['cadastre']['features'][:1]}
        page.locator('[name="rpg_year"]').fill('2023')
        page.locator('#rpg-file').set_input_files({
            'name':'rpg-2023.geojson','mimeType':'application/geo+json',
            'buffer':json.dumps(sample).encode('utf-8'),
        })
        territory.click()
        page.locator('#project-name').fill('Projet démonstration')
        page.locator('#surface').fill('12000')
        assert not page.locator('#radius-value').is_visible()
        page.screenshot(path=output/'workspace-prepare.png',full_page=True)
        page.locator('#continue-to-map').click()
        assert page.locator('body').get_attribute('data-view')=='carte'
        assert page.locator('#map').is_visible()
        assert page.locator('#analyze-button').is_disabled()
        page.locator('#map-setup #radius-value').fill('15')
        page.locator('#radius-value').press('Tab')
        assert page.locator('#radius').input_value()=='15'
        page.go_back()
        page.wait_for_function("document.body.dataset.view==='projet'")
        assert page.locator('#surface').input_value()=='12000'
        page.go_forward()
        page.wait_for_function("document.body.dataset.view==='carte'")
        assert page.locator('#radius-value').input_value()=='15'
        page.locator('#map-setup .gps-entry > summary').click()
        page.locator('#lat').fill('43.61');page.locator('#lon').fill('3.88');page.locator('#lon').press('Tab')
        page.get_by_role('button',name='Utiliser ce point GPS',exact=True).click()
        assert page.locator('#analyze-button').is_enabled()
        page.screenshot(path=output/'workspace-point.png',full_page=True)
        page.locator('#analyze-button').click()
        page.locator('[data-progress-step="sites"] progress:not([value])').wait_for(timeout=20000)
        assert '1 / 3' in page.locator('#progress-count').inner_text()
        assert page.locator('.workspace-nav [data-view="projet"]').is_disabled()
        page.screenshot(path=output/'workspace-loading.png',full_page=True)
        page.locator('.scenario-card.selected').wait_for(timeout=20000)
        assert submitted[0]['surface']==12000
        assert submitted[0]['radius']==15
        assert submitted[0]['rpg_year']==2023 and submitted[0]['rpg_geojson']==sample
        assert submitted[0]['lat']==43.61 and submitted[0]['lon']==3.88
        assert page.locator('.scenario-card:visible').count()==1
        assert not page.locator('#map-setup').is_visible()
        assert page.locator('[data-layer="network"]').get_attribute('aria-pressed')=='true'
        assert page.get_by_text('Acquisition estimée',exact=True).is_visible()
        assert page.evaluate("""async data => {
          const {retainedCadastre}=await import('/assets/map.js');
          const retained=retainedCadastre(data);
          return retained.length===1 && retained[0].properties.idu!=='unused-parcel';
        }""", payload)
        congestion=page.get_by_role('button',name='Congestion HTA/BT')
        assert congestion.get_attribute('aria-pressed')=='true'
        assert page.locator('.leaflet-congestion-pane canvas').count()==1
        assert page.locator('#congestion-legend').is_visible()
        congestion.click()
        assert not page.locator('#congestion-legend').is_visible()
        page.get_by_role('button',name='Voir les indicateurs économiques').click()
        try:page.locator('.economic-kpi').first.wait_for(timeout=8000)
        except BrowserTimeout:
            print(json.dumps(dict(errors=errors,view=page.locator('body').get_attribute('data-view'),economics=page.locator('#economics-page').inner_text()),ensure_ascii=True))
            raise
        assert page.locator('.economic-kpi').count()==4
        assert page.get_by_text('Acquisition foncière incluse dans le CAPEX.',exact=False).is_visible()
        assert page.locator('.economic-chart svg').count()==2
        assert not page.locator('#map').is_visible()
        page.locator('#economics-form [name="sale_price_eur_mwh"]').fill('90')
        page.locator('.workspace-nav [data-view="carte"]').click()
        page.locator('.workspace-nav [data-view="economie"]').click()
        assert page.locator('#economics-form [name="sale_price_eur_mwh"]').input_value()=='90'
        page.locator('#economics-form [name="sale_price_eur_mwh"]').fill('0')
        assert page.locator('.economics-results.is-stale').count()==1
        page.locator('#economics-form button[type="submit"]').click()
        page.wait_for_function("document.querySelector('.economic-kpi:nth-child(2) strong')?.textContent.includes('Non atteint')")
        assert financial_posts[-1]['sale_price_eur_mwh']==0
        page.locator('#economics-form [name="sale_price_eur_mwh"]').fill('80')
        page.locator('#economics-form button[type="submit"]').click()
        page.locator('.economics-results:not(.is-stale)').wait_for()
        page.screenshot(path=output/'workspace-economics.png',full_page=True)
        assert len(submitted)==1
        page.get_by_role('button',name='Bilan technique',exact=True).click()
        assert not page.locator('#map').is_visible()
        page.locator('.compare-results > summary').click()
        assert page.locator('.scenario-card:visible').count()==3
        page.locator('[data-tab="electricity"]').click()
        page.get_by_text('630 A',exact=True).wait_for()
        page.screenshot(path=output/'workspace-report.png',full_page=True)
        page.get_by_role('button',name='Repérer le raccordement sur la carte').click()
        assert page.locator('#map').is_visible()
        page.screenshot(path=output/'workspace-map.png',full_page=True)
        page.locator('.workspace-nav [data-view="projet"]').click()
        assert page.locator('#surface').input_value()=='12000'
        page.set_viewport_size(dict(width=390,height=844))
        assert not page.locator('#map').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=output/'workspace-mobile.png',full_page=True)
        page.locator('#continue-to-map').click()
        assert page.locator('#map').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=output/'workspace-mobile-map.png',full_page=True)
        page.locator('.workspace-nav [data-view="economie"]').click()
        page.locator('.economic-kpi').first.wait_for()
        assert page.locator('.economic-kpi').count()==4
        assert page.locator('#economics-page').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        page.screenshot(path=output/'workspace-economics-mobile.png',full_page=True)
        assert not errors,errors
        browser.close()
    print('Workspace OK: guided pages, economic KPIs/charts, recalculation, draft retention, loading and mobile.')


if __name__=='__main__':main()

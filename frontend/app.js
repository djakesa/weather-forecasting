import {api,waitForJob} from './api.js';
import {ProjectMap, CONGESTION_ZONES} from './map.js';
import {icon,mountIcons} from './icons.js';
import {setupWorkspace} from './workspace.js';
import {createEconomicsPage} from './economics.js';

const $=id=>document.getElementById(id);
const number=(value,digits=0)=>value==null?'—':new Intl.NumberFormat('fr-FR',{maximumFractionDigits:digits}).format(value);
const money=value=>value==null?'Non chiffré':value>=1e6?`${number(value/1e6,2)} M€`:`${number(value/1000,0)} k€`;
const euros=value=>new Intl.NumberFormat('fr-FR',{style:'currency',currency:'EUR',maximumFractionDigits:0}).format(value);
const distance=value=>value==null?'—':value>=1000?`${number(value/1000,2)} km`:`${number(value)} m`;
const state={config:null,result:null,job:null,busy:false,tab:'overview',scenario:2,dirty:false,layers:{solar:true,network:false,pv:false},search:''};
let toastTimer;

function node(tag,className='',text){const el=document.createElement(tag);el.className=className;if(text!==undefined)el.textContent=text;return el;}
function iconNode(name,className=''){const el=node('span',className);el.innerHTML=icon(name);return el;}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?10000:5000);}
function error(message){$('form-error').textContent=message;$('form-error').hidden=Boolean(!message);}
const map=new ProjectMap((lat,lon)=>{if(!state.busy)setCoordinates(lat,lon,true);},feature=>inspectAsset(feature));
const workspace=setupWorkspace((view,picking)=>{map.map.invalidateSize({pan:false});if(view==='carte'&&picking&&!state.busy){map.setEditing(true);map.fit();}});
const economics=createEconomicsPage({request:api,navigate:workspace.go});

function input(name){return $('project-form').elements.namedItem(name);}
function selection(){return{lat:Number($('lat').value),lon:Number($('lon').value),radius:Number($('radius').value)};}
function updateSelection(fit=true){
  const {lat,lon,radius}=selection();
  if(!Number.isFinite(lat)||!Number.isFinite(lon)||lat<41||lat>51.5||lon< -5.5||lon>10||radius<.1||radius>1000)return;
  map.setSelection(lat,lon,radius,fit);
  $('selection-label').textContent=`${number(Math.abs(lat),5)}° ${lat>=0?'N':'S'} · ${number(Math.abs(lon),5)}° ${lon>=0?'E':'O'}`;
  $('radius-label').textContent=`${number(radius,1)} km`;
  $('radius-value').value=radius;
}
function setCoordinates(lat,lon,fit){$('lat').value=lat.toFixed(5);$('lon').value=lon.toFixed(5);workspace.confirmPoint();markDirty();updateSelection(fit);}
function markDirty(){if(state.result){state.dirty=true;economics.markProjectDirty();$('results-title').textContent='Paramètres modifiés';$('project-heading').textContent='Paramètres modifiés · relancez l’analyse';}updatePower();}
function updatePower(){const kwp=Number($('surface').value)*Number($('density').value)/1000;$('power-preview').replaceChildren(document.createTextNode(number(kwp/1000,2)+' '),node('small','', 'MWc'));}
function setLayer(name,visible){
  state.layers[name]=visible;map.toggle(name,visible);
  const button=document.querySelector(`[data-layer="${name}"]`);
  button.classList.toggle('active',visible);button.setAttribute('aria-pressed',String(visible));
  renderCongestionLegend();$('solar-legend').hidden=!state.result?.raster||!state.layers.solar;
}
function renderCongestionLegend(){
  const legend=$('congestion-legend');
  legend.hidden=!state.layers.congestion;
  if(legend.hidden)return;
  legend.replaceChildren(node('strong','','Congestion Enedis · HTA/BT'));
  if(!state.result?.grid_constraints?.features?.length){
    legend.append(node('small','','Aucune zone Enedis affichable pour ce résultat.'));
    return;
  }
  for(const [label,color] of Object.values(CONGESTION_ZONES)){
    const row=node('p','congestion-key');const swatch=node('i');swatch.style.backgroundColor=color;
    row.append(swatch,document.createTextNode(label));legend.append(row);
  }
  legend.append(node('small','','Capacité d’injection à confirmer.'));
}
function resetProject(){
  if(state.busy){toast('Annulez le calcul en cours avant de recommencer.');return;}
  workspace.reset();
  economics.setProject(null);
  state.result=null;state.dirty=false;state.tab='overview';state.scenario=2;state.search='';
  $('project-form').reset();fillForm(state.config.defaults);map.clear();map.setEditing(true);
  for(const id of ['results-panel','asset-inspector','metrics-strip','solar-legend','reopen-results','progress-card'])$(id).hidden=true;
  $('map-intro').hidden=false;$('export-button').disabled=true;$('results-content').replaceChildren();$('progress-log').textContent='';
  $('project-heading').textContent='Nouveau projet photovoltaïque';error('');
  setLayer('solar',true);setLayer('network',false);setLayer('pv',false);setLayer('constraints',false);setLayer('congestion',false);
  const url=new URL(location.href);url.searchParams.delete('project');history.replaceState(null,'',url);
  updateSelection(true);toast('Prêt pour une nouvelle analyse. Votre historique et les données en cache sont conservés.');
}
function updateYears(){
  const start=Number($('start').value);for(const option of $('end').options)option.disabled=Number(option.value)<start;
  if(Number($('end').value)<start)$('end').value=String(start);
  $('weather-period-help').textContent=start<2025?'GFS sur toute la période choisie, faute d’archive AROME complète avant 2025.':'AROME France · années complètes uniquement. Le budget météo est indépendant du rayon.';
}
function chosenVoltages(){return [...document.querySelectorAll('[data-grid-voltage]:checked')].map(el=>Number(el.value));}
function updateVoltageHelp(){const values=chosenVoltages();$('voltage-help').textContent=!values.length?'Sélectionnez au moins une tension avant de lancer le calcul.':values.some(v=>v>50)?'HTB sélectionnée : présélection possible, étude et devis spécifiques nécessaires. Le coût total ne sera pas extrapolé depuis la distribution.':'Seules ces tensions publiées peuvent être retenues. La tension ne prouve pas une capacité d’injection disponible.';}
async function refreshSolarCache(){try{const data=await api('/api/solar-cache');$('solar-cache-status').textContent=data.available?`Base solaire SQLite · ${number(data.points)} points stockés sur cet ordinateur.`:'Base locale temporairement indisponible ; les autres caches restent utilisables.';}catch{$('solar-cache-status').textContent='Base solaire SQLite · état non disponible.';}}
function setBusy(busy){state.busy=busy;for(const el of $('project-form').elements)el.disabled=busy;workspace.busy(busy);$('cancel-job').disabled=false;$('progress-card').hidden=!busy;if(busy){map.setEditing(false);$('map-intro').hidden=true;$('solar-legend').hidden=true;}else{$('solar-legend').hidden=!state.result?.raster||!state.layers.solar;if(!state.result)map.setEditing(true);}}

const costs=[['Installation PV (€/kWc)','pv',650,1200],['Liaison externe (€/m)','cable',80,250],['Collecteur interne (€/m)','collector',40,120],['Interface unique (€)','fixed',20000,100000],['Transformateur neuf (€)','transformer',30000,80000]];
for(const [label,key,low,high] of costs){const row=node('label','cost-row');row.append(node('span','',label));const fields=node('div');for(const [side,value] of [['low',low],['high',high]]){const field=node('input');field.type='number';field.name=`${key}_${side}`;field.value=value;field.min=0;field.max=key==='pv'?10000:['cable','collector'].includes(key)?5000:10000000;field.setAttribute('aria-label',`${label} — ${side==='low'?'bas':'haut'}`);fields.append(field);}row.append(fields);$('cost-inputs').append(row);}

async function payload(){
  if(!$('project-form').reportValidity())throw new Error('Vérifiez les champs du projet.');
  const data={};
  for(const el of $('project-form').elements){if(!el.name)continue;if(el.type==='checkbox')data[el.name]=el.checked;else if(el.type==='number'||el.type==='range'){if(el.value!=='')data[el.name]=Number(el.value);}else data[el.name]=el.value;}
  const cadastre=$('cadastre-file').files[0];
  const inventory=$('inventory-file').files[0];
  for(const [file,key,label] of [[cadastre,'cadastral_geojson','Cadastre'],[$('rpg-file').files[0],'rpg_geojson','RPG']]){
    if(!file)continue;
    if(file.size>10_000_000)throw new Error(label+' : réduisez l’extrait à 10 Mo.');
    try{data[key]=JSON.parse((await file.text()).replace(/^\uFEFF/,''));}
    catch{throw new Error(label+' : le fichier doit être un GeoJSON valide.');}
  }
  if(inventory){if(inventory.size>2_000_000)throw new Error('Le CSV électrique dépasse 2 Mo.');data.inventory_csv=await inventory.text();}
  data.start=Number(data.start);data.end=Number(data.end);
  data.weather_reuse_m=Number(data.weather_reuse_m);
  data.connection_voltages_kv=chosenVoltages();
  if(!data.connection_voltages_kv.length)throw new Error('Cochez au moins une tension de raccordement.');
  return data;
}

async function run(kind){
  if(state.busy)return;
  if(!workspace.canRun()){workspace.continue();return;}
  error('');
  try{
    let data;
    if(kind==='analysis')data=await payload();
    else{if(!$('lat').reportValidity()||!$('lon').reportValidity())return;data={...selection(),country:$('country').value,connection_voltages_kv:chosenVoltages()};if(!data.connection_voltages_kv.length)throw new Error('Cochez au moins une tension de raccordement.');data.radius=Math.min(data.radius,3);}
    setBusy(true);$('progress-phase').textContent=kind==='analysis'?'Analyse du territoire':'Exploration du réseau';$('progress-message').textContent='Préparation des données…';$('progress-log').textContent='';
    const job=await api(kind==='analysis'?'/api/analyses':'/api/network',{method:'POST',body:JSON.stringify(data)});
    state.job=job.id;
    const result=await waitForJob(job.id,status=>{renderLoading(status);$('progress-phase').textContent=status.phase;$('progress-message').textContent=({Météo:'Nous évaluons l’ensoleillement de votre zone.',Réseau:'Nous recherchons les ouvrages et un raccordement possible.',Parcelles:'Nous recherchons les terrains et organisons les panneaux.',Préparation:'Nous préparons votre projet.'})[status.phase]||status.message;$('progress-log').textContent=status.logs.map(row=>`${new Date(row.time).toLocaleTimeString('fr-FR')} · ${row.message}`).join('\n');if(status.status==='cancelling')$('cancel-job').disabled=true;});
    showResult(result);
    refreshSolarCache();
    toast(kind==='analysis'?'Analyse terminée. Les résultats sont enregistrés sur cet ordinateur.':`${result.summary.network_count} ouvrages chargés dans un rayon de ${number(data.radius,1)} km.`);
  }catch(exc){const cancelled=exc.message.includes('annulée');error(cancelled?'':exc.message);toast(exc.message,!cancelled);}finally{state.job=null;setBusy(false);}
}

function renderLoading(status){
  const data=status.progress;
  document.querySelectorAll('[data-progress-step]').forEach(row=>{const step=data?.steps?.find(s=>s.key===row.dataset.progressStep);row.hidden=Boolean(data&&!step);const bar=row.querySelector('progress');if(step?.state==='running')bar.removeAttribute('value');else bar.value=step?.state==='done'?1:0;row.dataset.state=step?.state||'pending';});
  $('progress-count').textContent=data?data.completed_steps+' / '+data.total_steps+' étapes terminées':'Chargement des données…';
}

function showResult(result){
  if(result.kind==='analysis'&&result.summary?.placed_m2!=null&&result.summary.placed_m2<=1&&result.scenarios?.[2])result.scenarios[2].unavailable=true;
  if(result.kind==='analysis'&&!(result.pv?.features?.length)&&result.scenarios?.[2])result.scenarios[2].unavailable=true;
  state.result=result;state.dirty=false;state.scenario=result.scenarios?.[2]&&!result.scenarios[2].unavailable?2:result.scenarios?.[1]&&!result.scenarios[1].unavailable?1:0;
  economics.setProject(result);workspace.ready(result);
  state.tab=result.kind==='network'?'electricity':'overview';state.search='';
  $('project-heading').textContent=result.name;$('results-title').textContent=result.kind==='network'?'Ouvrages autour du point':'Résultats de l’analyse';
  $('results-panel').hidden=false;$('reopen-results').hidden=true;$('map-intro').hidden=true;$('export-button').disabled=false;
  map.setData(result);
  setLayer('constraints',false);
  setLayer('congestion',Boolean(result.grid_constraints?.features?.length));
  setLayer('network',true);
  setLayer('solar',result.kind!=='network');
  setLayer('pv',result.kind!=='network');
  if(result.raster){$('legend-min').textContent=number(result.raster.min);$('legend-max').textContent=number(result.raster.max);}
  $('electricity-count').textContent=result.network?.features.length||0;
  $('solar-legend').hidden=!result.raster||!state.layers.solar;
  $('asset-inspector').hidden=true;renderMetrics();renderResults();
  const url=new URL(location.href);url.searchParams.set('project',result.id);history.replaceState(null,'',url);
}

function metric(label,value,unit,note){const el=node('div','metric');el.append(node('label','',label));const strong=node('strong','',value);if(unit)strong.append(node('small','',unit));el.append(strong);if(note)el.append(node('span','metric-note',note));return el;}
function renderMetrics(){
  const data=state.result;$('metrics-strip').replaceChildren();$('metrics-strip').hidden=!data;
  if(data.kind==='network'){$('metrics-strip').append(metric('OUVRAGES RECENSÉS',number(data.summary.network_count),'','Ouvrages publics · sources détaillées'),metric('RAYON DU RÉSEAU',number(data.config.radius,1),'km','Enedis et sources complémentaires'));return;}
  const scenario=data.scenarios[state.scenario];
  $('metrics-strip').append(metric('PRODUCTION ESTIMÉE',scenario.unavailable?'—':number(scenario.energy/1000,1),'MWh/an','Selon le scénario sélectionné'),metric('ENVELOPPE CAPEX HT',scenario.unavailable?'—':money(scenario.cost.low),'',scenario.cost.high!=null?`à ${money(scenario.cost.high)}`:'Raccordement à compléter'),metric('PUISSANCE CIBLE',number(data.summary.kwp/1000,2),'MWc',`${number(data.summary.surface)} m² de modules`),metric('SURFACE PLACÉE',number(data.summary.placed_m2),'m²',data.summary.missing_m2>1?`${number(data.summary.missing_m2)} m² non placés`:'Sur emprises cadastrales'));
}

function info(label,value){const el=node('div','info-line');el.append(node('span','',label),node('strong','',value??'Non renseigné'));return el;}
function renderResults(){
  document.querySelectorAll('[data-tab]').forEach(button=>button.classList.toggle('active',button.dataset.tab===state.tab));
  const container=$('results-content');container.replaceChildren();
  if(state.tab==='overview')renderOverview(container);
  if(state.tab==='electricity')renderElectricity(container);
  if(state.tab==='parcels')renderParcels(container);
  if(state.tab==='constraints')renderConstraints(container);
}
function renderOverview(container){
  const data=state.result;if(!data.scenarios){container.append(node('p','empty-result','Lancez une analyse solaire pour comparer les implantations.'));return;}
  container.append(node('p','result-intro',state.dirty?'Paramètres modifiés : relancez la recherche pour les appliquer.':'Voici le résultat de votre recherche. Les autorisations et le raccordement restent à confirmer.'));
  if(data.scenarios[2]?.unavailable){
    const note=node('article','issue-item');note.append(node('strong','mini-heading',data.summary.placed_m2===0?'Aucune implantation exploitable trouvée':'Analyse parcellaire à compléter'),node('p','result-note','Le potentiel solaire est conservé. Vous pouvez modifier le secteur ou le rayon cadastral, puis relancer avec les mêmes données météo en cache.'));
    const retry=node('button','button secondary','Relancer ce projet');retry.type='button';retry.disabled=state.busy;retry.onclick=()=>run('analysis');note.append(retry);container.append(note);
  }
  const names=['Meilleur ensoleillement','Proximité du réseau','Implantation proposée'];
  if(data.summary.placed_m2!=null)container.append(info('Panneaux implantés',number(data.summary.placed_m2)+' m² sur '+number(data.summary.surface)+' m²'));
  if(state.scenario===2&&data.parcels?.length){
    const priced=data.parcels.filter(parcel=>parcel.achat_estime_eur!=null);
    const total=priced.reduce((sum,parcel)=>sum+parcel.achat_estime_eur,0);
    if(priced.length)container.append(info(priced.length===data.parcels.length?'Acquisition estimée':'Acquisition estimée · part chiffrée',euros(total)));
    else if(data.config?.price_mode==='Ventes locales comparables')container.append(info('Acquisition estimée','À chiffrer'));
  }
  const comparison=node('details','compare-results');comparison.append(node('summary','','Comparer les deux autres scénarios'));
  data.scenarios.forEach((scenario,index)=>{
    const card=node('div',`scenario-card ${state.scenario===index?'selected':''}`);card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label',names[index]);
    const title=node('div','scenario-title');const name=node('span');name.append(node('span','scenario-index',String(index+1).padStart(2,'0')),document.createTextNode(names[index]));title.append(name,node('span',`status-badge ${scenario.unavailable?'muted':''}`,scenario.unavailable?'INDISPONIBLE':index===2?'FONCIER':'PRÉSÉLECTION'));card.append(title);
    const values=node('div','scenario-values');
    for(const [label,value] of [['Production',scenario.unavailable?'Non calculée':`${number(scenario.energy/1000,1)} MWh/an`],['CAPEX HT',scenario.unavailable?'Non calculé':scenario.cost.low==null?'À compléter':`${money(scenario.cost.low)} — ${money(scenario.cost.high)}`]]){const cell=node('div');cell.append(node('label','',label),node('strong','',value));values.append(cell);}card.append(values);
    card.append(node('small','scenario-note',scenario.unavailable?(index===2&&data.summary.placed_m2===0?'Aucune emprise exploitable trouvée. Consultez les secteurs étudiés.':'Calcul à compléter : voir le bilan des sources.'):scenario.land_included?'Acquisition foncière incluse':'Foncier à compléter'));
    const select=()=>{state.scenario=index;renderMetrics();renderResults();map.focusScenario(scenario);if(index===2)setLayer('pv',true);};card.onclick=select;card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}};(state.scenario===index?container:comparison).append(card);
  });
  container.append(comparison);
  const economicsButton=node('button','button secondary','Voir les indicateurs économiques');
  economicsButton.type='button';
  economicsButton.onclick=()=>workspace.go('economie');
  container.append(economicsButton);
  if(data.collector?.connections){container.append(node('h3','mini-heading','Raccordement commun'),info('Collecteur entre champs',distance(data.collector.collector_length_m)),info('Liaison externe unique',distance(data.collector.external_length_m)),info('Transformateur projet calculé',`${number(data.collector.sizing.project_transformer_kva)} kVA`));}
  const selected=data.scenarios[state.scenario];
  const technical=node('details','technical-summary');technical.append(node('summary','','Données et hypothèses de calcul'));container.append(technical);
  if(!selected.unavailable){const details=node('details');details.append(node('summary','mini-heading','Détail de l’enveloppe CAPEX'));for(const [label,key] of [['Installation PV','pv'],['Collecteur, liaison et interface','connection'],['Transformateur neuf','transformer'],['Acquisition foncière','land']])details.append(info(label,key==='land'&&!selected.land_included?'À compléter':`${money(selected.cost[key+'_low'])} — ${money(selected.cost[key+'_high'])}`));details.append(node('p','result-note',`Travaux avant provision de ${number((data.assumptions?.contingency||0)*100)} %. Foncier hors frais d’acte et fiscalité ; renforcement réseau non chiffré.`));container.append(details);}
  if(data.monthly_irradiance?.length===12){technical.append(node('h3','mini-heading','Profil solaire · kWh/m²/mois'));const chart=node('div','monthly-chart');const max=Math.max(...data.monthly_irradiance,1);data.monthly_irradiance.forEach((value,index)=>{const column=node('div','month-column');const bar=node('i');bar.style.height=`${Math.max(2,value/max*100)}%`;column.title=`${['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre'][index]} : ${number(value,1)} kWh/m²`;column.append(bar,node('span','',['J','F','M','A','M','J','J','A','S','O','N','D'][index]));chart.append(column);});technical.append(chart);}
  technical.append(info('Modèle météo',data.summary.weather_model),info('Points météo réutilisés',`${data.summary.weather_cache_hits} / ${data.summary.weather_points}`),node('p','result-note','Présélection indicative. Les puissances de plaque, le dimensionnement du projet et la capacité disponible d’injection sont des données distinctes.'));
  if(data.summary.weather_db_hits!=null)technical.append(info('Réutilisés depuis SQLite',number(data.summary.weather_db_hits)),info('Dont points voisins estimés',number(data.summary.weather_nearby_hits)),info('Nouveaux points Open-Meteo',number(data.summary.weather_api_points)));
  if(data.config.connection_voltages_kv)technical.append(info('Tensions acceptées',data.config.connection_voltages_kv.map(v=>`${number(v,1)} kV`).join(', ')));
  appendWarnings(container,data.warnings);
  if(data.activity?.length){const details=node('details');details.append(node('summary','mini-heading','Voir le déroulement de cette analyse'));const log=node('pre','activity-log',data.activity.map(row=>`${new Date(row.time).toLocaleTimeString('fr-FR')} · ${row.message}`).join('\n'));details.append(log);container.append(details);}
}
function appendWarnings(container,warnings=[]){
  if(!warnings.length)return;
  const details=node('details');details.append(node('summary','mini-heading','Points à vérifier ('+warnings.length+')'));
  const groups=new Map();
  for(const text of new Set(warnings)){
    let friendly=text;
    let title='À vérifier';
    if(/^Réseau indisponible|^Aucun actif électrique|^Aucun ouvrage électrique|^Aucun point de raccordement/i.test(text)){title='Raccordement';friendly=/^Réseau indisponible/i.test(text)?'Les sources réseau n’ont pas répondu. Le raccordement reste à étudier.':'Les sources interrogées ne permettent pas de confirmer un raccordement dans ce secteur. Cela ne prouve pas l’absence de réseau.';}
    else if(/^Cadastre indisponible/i.test(text)){title='Parcelles';friendly='Les parcelles de ce secteur n’ont pas pu être récupérées. Le résultat solaire reste disponible.';}
    else if(/^Contexte du terrain indisponible/i.test(text)){title='Terrain';friendly='Les obstacles physiques n’ont pas pu être vérifiés. L’implantation reste à confirmer.';}
    else if(/^Contraintes/i.test(text))title='Réglementation';
    const key=title==='À vérifier'?friendly:title;
    if(!groups.has(key))groups.set(key,{title,friendly,messages:[]});
    groups.get(key).messages.push(text);
  }
  for(const group of groups.values()){
    const item=node('article','issue-item');item.append(node('strong','mini-heading',group.title),node('p','warning-note',group.friendly));
    if(group.messages.length>1||group.messages[0]!==group.friendly){const technical=node('details');technical.append(node('summary','field-help','Détails et sources'));for(const text of group.messages)technical.append(node('p','result-note',text));item.append(technical);}details.append(item);
  }container.append(details);
}

function renderConstraints(container){
  renderGridConstraints(container);
  const data=state.result;const checks=data.regulatory?.checks||[];
  const hasLand=(data.summary?.placed_m2||0)>1;
  container.append(node('p','result-intro',hasLand?'Des emprises ont été proposées. Voici les contrôles effectués et les validations restant à obtenir.':'Aucune implantation exploitable trouvée dans les secteurs étudiés. Le bilan ci-dessous explique les contrôles effectués et les limites rencontrées.'));
  if(!checks.length)container.append(node('p','warning-note','Les données territoriales sont indisponibles pour ce résultat. Consultez les motifs ci-dessous.'));
  const quiet=node('details');quiet.append(node('summary','mini-heading','Contrôles sans objet recensé'));
  for(const check of checks){const card=node('article','issue-item');card.append(node('strong','mini-heading',check.name),info('Contrôle',{checked:'Données consultées',unavailable:'Service indisponible',unknown:'À vérifier',declared:'Déclaré · justificatif à contrôler',missing:'Justificatif manquant',blocked:'Critère non respecté',declared:'Déclaré · justificatif à contrôler',missing:'Justificatif manquant',blocked:'Critère non respecté'}[check.status]||'À vérifier'),node('p','result-note',check.note));if(check.count!=null)card.append(info('Objets trouvés',number(check.count)));if(check.zones?.length)card.append(info('Zonages',check.zones.join(', ')));if(check.source_url){try{const url=new URL(check.source_url);if(url.protocol==='https:'){const link=node('a','source-link','Source officielle ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}}catch{}}(check.status==='checked'&&check.count===0?quiet:container).append(card);}
  if(quiet.children.length>1)container.append(quiet);
  if(data.regulatory?.decisions?.length){container.append(node('h3','mini-heading','Filtres appliqués'));for(const decision of data.regulatory.decisions)container.append(node('p','result-note',decision));}
  if(data.site_comparison?.length){container.append(node('h3','mini-heading','Secteurs étudiés'));for(const site of data.site_comparison){const usable=(site.placed_m2||0)>1;container.append(info(`Secteur ${site.index}${site.chosen&&usable?' · proposé':''}`,site.placed_m2==null?'Analyse incomplète':usable?`${number(site.placed_m2)} m² de modules`:'Aucune surface exploitable'));if(site.weather_distance_m>1)container.append(node('p','result-note',`Irradiance estimée depuis le point météo le plus proche, à ${distance(site.weather_distance_m)}. À confirmer avant décision.`));renderSectorDetail(container,site);for(const issue of site.issues||[])container.append(node('p','result-note',issue));}}
}

function knownVoltage(p){return (p.voltage_label||'').includes('kV');}
function renderSectorDetail(container,site){
  if(site.decision)container.append(info('Décision',{accepted:'Surface et cible réseau identifiées',no_connection:'Cible réseau non identifiée · secteur suivant',insufficient_land:'Surface insuffisante · secteur suivant',source_unavailable:'Sources incomplètes · secteur suivant'}[site.decision]||site.decision));
  if(site.search_expanded)container.append(node('p','result-note',`Recherche élargie · potentiel solaire inférieur de ${number(site.solar_loss_percent,1)} % au meilleur point météo.`));
  const items=(site.land_diagnostics||[]).filter(item=>item.removed_m2>1);
  if(items.length){const details=node('details');details.append(node('summary','mini-heading','Surfaces écartées par filtre'));for(const item of items)details.append(info(item.reason,`${number(item.removed_m2)} m²`));container.append(details);}
}
function renderGridConstraints(container){
  const grid=state.result.grid_constraints;if(!grid)return;
  const card=node('details','issue-item');card.append(node('summary','mini-heading','État du réseau Enedis et sources'));
  const labels={SATUREE:'Rouge · saturée',EN_TENSION:'Orange · en tension',FAVORABLE:'Vert · favorable',TRES_FAVORABLE:'Vert · très favorable',ELD:'Gris · hors périmètre Enedis'};
  card.append(info('Secteur',grid.status==='unavailable'?'Information indisponible':(grid.statuses||[]).map(s=>labels[s]||s).join(' / ')||'Couverture inconnue'));
  if(grid.coverage_share!=null)card.append(info('Couverture Enedis du secteur',`${number(100*grid.coverage_share)} %`));
  const meta=grid.metadata||{};if(meta.publication)card.append(info('Publication',meta.publication));
  if(meta.snapshot_date)card.append(info('Version de la copie',new Date(meta.snapshot_date).toLocaleDateString('fr-FR')));
  if(meta.retrieved_at)card.append(info('Récupérée le',new Date(meta.retrieved_at).toLocaleDateString('fr-FR')));
  if(!meta.operator_update_date&&grid.status!=='unavailable')card.append(node('p','result-note','Date de mise à jour Enedis non certifiée par le fichier. La date de version identifie la copie consultée.'));
  if(grid.stale)card.append(node('p','warning-note','Version de plus de 90 jours : actualisation à vérifier.'));
  card.append(node('p','result-note',grid.note||'Capacité disponible à confirmer par Enedis.'),node('p','result-note','Les couleurs sont visibles avec la couche Réseau électrique. Les zones blanches restent inconnues.'));
  const link=node('a','source-link','Carte et définitions Enedis ↗');link.href='https://observatoire.enedis.fr/services/carte-zones-contrainte-projets-enr';link.target='_blank';link.rel='noopener noreferrer';card.append(link);container.append(card);
}
function renderElectricity(container){
  renderCableConnection(container);
  renderGridConstraints(container);
  const all=state.result.network?.features||[];
  const withVoltage=all.filter(f=>knownVoltage(f.properties)).length;const withRating=all.filter(f=>f.properties.power_kva!=null).length;
  if(state.result.config?.connection_voltages_kv){const compatible=all.filter(f=>f.properties.voltage_selection_compatible&&f.properties.planning_eligible!==false).length;container.append(info('Cibles compatibles avec les tensions cochées',`${compatible} / ${all.length}`));}
  const sources=[...new Set(all.map(f=>f.properties.source).filter(Boolean))].join(', ')||'Open Infrastructure Map et sources officielles de secours';
  container.append(node('p','result-intro',`Sources : ${sources}. ${all.length} ouvrages, dont ${withVoltage} avec tension publiée et ${withRating} avec puissance assignée. Cliquez sur un ouvrage pour voir ses valeurs.`));
  const search=node('input','asset-search');search.type='search';search.placeholder='Rechercher un nom, une tension…';search.value=state.search;search.setAttribute('aria-label','Rechercher un ouvrage');container.append(search);
  const rows=node('div');container.append(rows);
  const undocumented=node('input');undocumented.type='checkbox';const option=node('label','check-line');option.append(undocumented,document.createTextNode('Inclure les ouvrages sans tension publiée ('+(all.length-withVoltage)+')'));container.insertBefore(option,rows);
  const draw=()=>{rows.replaceChildren();const query=state.search.toLowerCase();const filtered=all.filter(feature=>(undocumented.checked||knownVoltage(feature.properties))&&JSON.stringify(feature.properties).toLowerCase().includes(query)).sort((a,b)=>Number(knownVoltage(b.properties))-Number(knownVoltage(a.properties)));
    for(const feature of filtered.slice(0,150)){const p=feature.properties;const row=node('button','asset-row');row.type='button';row.append(iconNode(p.kind==='transformer'||p.kind==='substation'?'network':'layers','asset-symbol'));const text=node('span');text.append(node('strong','',p.name||typeName(p.kind)),node('small','',p.id));row.append(text,node('span','voltage-chip',p.voltage_label||'Non publiée'));row.onclick=()=>{setLayer('network',true);map.inspect(feature);};rows.append(row);}
    if(!filtered.length)rows.append(node('p','empty-result','Aucun ouvrage correspondant. Essayez un autre terme ou une autre zone.'));
    if(filtered.length>150)rows.append(node('p','result-note',`${filtered.length-150} autres ouvrages. Affinez la recherche ; l’export contient l’ensemble de l’extrait.`));};
  undocumented.onchange=draw;
  search.oninput=()=>{state.search=search.value;draw();};draw();appendWarnings(container,state.result.warnings);
}
function renderParcels(container){
  const data=state.result;const rows=data.parcels||[];
  if(!rows.length){container.append(node('p','empty-result','Aucune emprise exploitable trouvée. Consultez les secteurs étudiés et les données territoriales.'));appendWarnings(container,data.warnings);return;}
  const parcelCount=new Set(rows.map(row=>row.reference_cadastrale||row.parcelle)).size;
  container.append(node('p','result-intro',`${parcelCount} parcelle${parcelCount>1?'s':''} retenue${parcelCount>1?'s':''} · ${rows.length} champ${rows.length>1?'s':''} PV. Une parcelle peut accueillir plusieurs champs.`));
  rows.forEach(p=>{
    const card=node('div','scenario-card');
    card.append(node('div','scenario-title',p.reference_cadastrale||p.parcelle),
      info('Modules placés',`${number(p.pv_m2)} m²`),info('Emprise au sol',`${number(p.emprise_m2)} m²`),
      info('Recouvrement RPG',p.part_agricole_pct==null?'Indisponible':`${number(p.part_agricole_pct,1)} %`),
      info('Chemin vers le réseau',distance(p.distance_m)));
    if(p.parcelle&&p.parcelle!==p.reference_cadastrale)card.append(info('Champ PV',p.parcelle));
    const labels=new Set(p.gpu_intersections?.map(item=>item.libelle||item.suptype||item.typezone||item.typesect||item.layer));
    if(labels.size)card.append(info('Couches GPU recoupées',[...labels].join(', ')));
    if(p.achat_estime_eur!=null){
      card.append(info('Acquisition estimée',p.surface_achat_m2===0?'Déjà comptée pour cette parcelle':euros(p.achat_estime_eur)),
        info('Surface d’achat',`${number(p.surface_achat_m2)} m²`),
        info('Référence foncière',`${number(p.reference_prix_foncier.eur_m2,2)} €/m²`));
      const reference=p.reference_prix_foncier;
      if(reference.count)card.append(node('p','result-note',
        `${reference.count} ventes comparables · ${reference.basis} · ${reference.reference_period} · ${reference.source}`));
    }else if(data.config?.price_mode==='Ventes locales comparables'){
      card.append(info('Acquisition estimée','Ventes comparables insuffisantes ou type inconnu'));
    }
    container.append(card);
  });
  if(data.summary.missing_m2>1)container.append(node('p','warning-note',`${number(data.summary.missing_m2)} m² de modules restent à placer. Les coûts concernent uniquement la surface placée.`));
}
function typeName(kind){return{transformer:'Transformateur',substation:'Poste électrique',line:'Ligne aérienne',minor_line:'Ligne aérienne',cable:'Câble souterrain'}[kind]||kind;}
function inspectAsset(feature){
  const p=feature.properties;const target=$('asset-details');target.replaceChildren(node('span','eyebrow',typeName(p.kind).toUpperCase()),node('h3','',p.name||typeName(p.kind)),node('div','voltage-hero',p.voltage_label||'Tension non publiée'));
  const rows=[['Identifiant',p.id],['Primaire',p.primary_voltage_v==null?null:`${number(p.primary_voltage_v/1000,3)} kV`],['Secondaire',p.secondary_voltage_v==null?null:`${number(p.secondary_voltage_v/1000,3)} kV`],['Puissance assignée',p.power_kva==null?'Non publiée':`${number(p.power_kva)} kVA`],['Gestionnaire',p.operator],['Source',p.source],['Précision',p.coordinate_precision],['Capacité d’injection',p.available_generation_mw==null?'Non publiée':`${number(p.available_generation_mw,2)} MW`]];
  for(const [label,value] of rows)if(value!=null)target.append(info(label,value));
  if(p.voltage_selection_reason)target.append(info('Filtre de tension',p.voltage_selection_reason));
  if(p.voltage_class)target.append(info('Catégorie publiée',p.voltage_class));
  if(p.data_scope)target.append(node('p','result-note',p.data_scope));
  if(p.enedis_zone_statuses?.length)target.append(info('Zone Enedis',p.enedis_zone_statuses.map(s=>({SATUREE:'Saturée',EN_TENSION:'En tension',FAVORABLE:'Favorable',TRES_FAVORABLE:'Très favorable',ELD:'Hors périmètre Enedis'})[s]||s).join(' / ')));
  if(p.planning_reason)target.append(node('p','result-note',p.planning_reason));
  if(p.source_url){try{const url=new URL(p.source_url);if(url.protocol==='https:'){const link=node('a','source-link','Consulter la source ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';target.append(link);}}catch{}}
  if(p.voltage_selection_matched_v?.length)target.append(info('Tensions compatibles publiées',p.voltage_selection_matched_v.map(v=>`${number(v/1000,1)} kV`).join(', ')));
  for(const [label,value] of [['Tertiaire',p.tertiary_voltage_v==null?null:`${number(p.tertiary_voltage_v/1000,3)} kV`],['Nombre de circuits',p.circuits],['Fréquence',p.frequency_hz==null?null:`${p.frequency_hz} Hz`],['Courant assigné',p.current_a==null?null:`${number(p.current_a)} A`]])if(value!=null)target.append(info(label,value));
  if(p.openinfra_url){try{const url=new URL(p.openinfra_url);if(url.protocol==='https:'&&url.hostname==='openinframap.org'){const link=node('a','source-link','Vérifier sur Open Infrastructure Map ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';target.append(link);}}catch{}}
  if(p.openinfra_properties){const details=node('details');details.append(node('summary','mini-heading','Attributs reçus d’Open Infrastructure Map'));for(const [key,value] of Object.entries(p.openinfra_properties))if(value!==null&&value!==undefined&&value!=='')details.append(info(key,String(value)));target.append(details);}
  if(p.osm_url){try{const url=new URL(p.osm_url);if(url.protocol==='https:'){const link=node('a','source-link','Consulter l’objet OpenStreetMap ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';target.append(link);}}catch{}}
  for(const [label,value] of [['Conducteurs électriques',p.cables],['Brins par conducteur',p.wires],['Matériau',p.conductor_material],['Section publiée',p.cross_section]])if(value!=null)target.append(info(label,String(value)));
  appendExactSource(target,p);
  target.append(node('p','result-note','Une tension publiée ne garantit pas la capacité de raccorder le projet. La géométrie de tuile reste une représentation simplifiée.'));$('asset-inspector').hidden=false;
}

const exactSourceCache=new Map();
function appendExactSource(container,p){
  const match=/^(?:OIM\/)?(node|way|relation)\/(\d+)$/.exec(p.id||'');if(!match)return;
  const box=node('details');box.open=true;box.append(node('summary','mini-heading','Caractéristiques de l’objet source'));
  const content=node('div');content.append(node('p','result-note','Lecture des attributs publiés…'));box.append(content);container.append(box);
  const key=match[1]+'/'+match[2];
  if(!exactSourceCache.has(key))exactSourceCache.set(key,api('/api/electrical/'+key).catch(error=>{exactSourceCache.delete(key);throw error;}));
  exactSourceCache.get(key).then(data=>{content.replaceChildren();for(const [label,value] of [['Tension',data.voltage_label],['Courant maximal publié',data.current_a==null?data.current_raw: number(data.current_a)+' A'],['Circuits',data.circuits],['Conducteurs électriques',data.cables],['Matériau',data.conductor_material],['Section (valeur source)',data.cross_section],['Puissance assignée',data.power_kva==null?null:number(data.power_kva)+' kVA']])if(value!=null)content.append(info(label,value));if(data.source_timestamp)content.append(info('Modification OSM',new Date(data.source_timestamp).toLocaleDateString('fr-FR')));content.append(node('p','result-note','Source : objet OpenStreetMap exact. Un champ absent reste non documenté ; ce courant ne représente pas la capacité d’injection disponible.'));}).catch(()=>{content.replaceChildren(node('p','result-note','La fiche source ne répond pas. Les valeurs déjà récupérées restent disponibles.'));});
}
function renderCableConnection(container){
  const data=state.result;const link=data.scenarios?.[2]?.connection||data.scenarios?.[1]?.connection;if(!link)return;
  const card=node('section','cable-details');card.append(node('h3','','Votre raccordement'),info('Ouvrage cible',link.id),info('Tension publiée',link.voltage_label),info('Liaison externe',distance(data.collector?.external_length_m??link.distance_m)));
  const asset=data.network?.features?.find(f=>f.properties.id===link.id);const p=asset?.properties||link;
  for(const [label,value] of [['Courant publié',p.current_a==null?null:number(p.current_a)+' A'],['Circuits',p.circuits],['Conducteurs électriques',p.cables],['Matériau',p.conductor_material],['Section publiée',p.cross_section]])if(value!=null)card.append(info(label,value));
  const button=node('button','button secondary','Repérer le raccordement sur la carte');button.onclick=()=>{workspace.go('carte');setLayer('network',true);setLayer('pv',true);if(asset)map.inspect(asset);};card.append(button);
  const route=data.routes?.features?.find(f=>f.properties.segment_type==='raccordement_unique');if(route){const props=route.properties;card.append(info('Collecteur neuf · tension de calcul',number(props.tension_collecteur_hypothese_kv)+' kV'),info('Collecteur neuf · courant calculé',number(props.intensite_collecteur_calculee_a)+' A'));if(props.liaison_basse_eur!=null&&props.liaison_haute_eur!=null)card.append(info('Liaison externe · coût estimatif HT',money(props.liaison_basse_eur)+' à '+money(props.liaison_haute_eur)));card.append(node('p','result-note','Dimensionnement et coûts indicatifs du projet, distincts des caractéristiques de la ligne existante.'));}
  appendExactSource(card,p);container.append(card);
}

async function openHistory(){
  $('history-list').replaceChildren(node('p','result-intro','Chargement…'));$('history-dialog').showModal();
  try{const list=await api('/api/projects');$('history-list').replaceChildren();if(!list.length)$('history-list').append(node('p','empty-result','Vos analyses apparaîtront ici. Elles sont enregistrées localement.'));
    for(const project of list){const button=node('button','history-item');const text=node('span');text.append(node('strong','',project.name),node('small','',`${new Date(project.created_at).toLocaleDateString('fr-FR')} · ${project.config?.country||'France'} · ${project.kind==='network'?'Réseau électrique':`${number(project.summary?.kwp/1000,2)} MWc`}`));button.append(text,iconNode('arrow'));button.onclick=async()=>{try{const data=await api(`/api/projects/${project.id}`);fillForm(data.config);showResult(data);$('history-dialog').close();updateSelection(true);}catch(exc){toast(exc.message,true);}};$('history-list').append(button);}
  }catch(exc){$('history-list').replaceChildren(node('p','warning-note',exc.message));}
}
function fillForm(config){$('project-form').reset();const values={...(state.config?.defaults||{}),...(config||{}),country:'France'};for(const [key,value] of Object.entries(values)){const el=input(key);if(el&&value!=null){if(el.type==='checkbox')el.checked=value;else el.value=value;}}document.querySelectorAll('[data-grid-voltage]').forEach(el=>el.checked=(values.connection_voltages_kv||[]).includes(Number(el.value)));$('radius-value').value=$('radius').value;updateVoltageHelp();updateYears();updatePower();updateSelection(false);}
function openExport(){const root=$('export-list');root.replaceChildren();for(const [label,path] of [['Rapport comparatif · CSV','report.csv'],['Emprises photovoltaïques · GeoJSON','pv.geojson'],['Ouvrages électriques · GeoJSON','network.geojson'],['Collecteur et raccordement · GeoJSON','connections.geojson'],['Points météo · GeoJSON','samples.geojson'],['Urbanisme et servitudes · GeoJSON','constraints.geojson'],['Cadastre et indicateurs territoriaux · GeoJSON','cadastre.geojson'],['Contraintes réseau Enedis · GeoJSON','grid-constraints.geojson']]){const link=node('a','export-item',label);link.href=`/api/projects/${state.result.id}/export/${path}`;link.append(iconNode('download'));root.append(link);}$('export-dialog').showModal();}

$('project-form').addEventListener('submit',event=>{event.preventDefault();if(document.body.dataset.view==='projet')workspace.continue();else run('analysis');});
$('connection-voltages').addEventListener('change',()=>{updateVoltageHelp();markDirty();});
$('reset-project').onclick=resetProject;
for(const id of ['start','end'])$(id).addEventListener('change',()=>{updateYears();markDirty();});
$('project-form').addEventListener('input',event=>{if(event.target.matches('[data-grid-voltage]'))updateVoltageHelp();markDirty();});
for(const id of ['lat','lon'])$(id).addEventListener('change',()=>{workspace.confirmPoint();updateSelection(true);});
$('radius').addEventListener('input',()=>{markDirty();updateSelection(false);});$('radius').addEventListener('change',()=>updateSelection(true));
$('radius-value').addEventListener('change',()=>{if($('radius-value').reportValidity()){$('radius').value=$('radius-value').value;updateSelection(true);markDirty();}});
$('country').addEventListener('change',()=>{const defaults=state.config.countries[$('country').value]?.default;if(defaults){$('lat').value=defaults[0];$('lon').value=defaults[1];}updateSelection(true);markDirty();});
$('network-button').onclick=()=>run('network');$('history-button').onclick=openHistory;$('export-button').onclick=openExport;
$('cancel-job').onclick=async()=>{if(state.job)try{await api(`/api/jobs/${state.job}`,{method:'DELETE'});$('cancel-job').disabled=true;toast('Annulation demandée.');}catch(exc){toast(exc.message,true);}};
document.querySelectorAll('[data-layer]').forEach(button=>button.onclick=()=>{setLayer(button.dataset.layer,!state.layers[button.dataset.layer]);if(button.dataset.layer==='network'&&state.layers.network&&!state.result&&!state.busy)run('network');});
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{state.tab=button.dataset.tab;state.search='';renderResults();});
document.querySelectorAll('[data-close-dialog]').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('basemap-button').onclick=()=>map.switchBasemap();$('zoom-in').onclick=()=>map.map.zoomIn();$('zoom-out').onclick=()=>map.map.zoomOut();$('fit-project').onclick=()=>map.fit();
$('edit-location').onclick=()=>{if(state.busy){toast('Le projet reste verrouillé pendant l’analyse.');return;}map.setEditing(!map.editing);toast(map.editing?'Cliquez sur la carte pour déplacer le centre.':'Sélection verrouillée : vous pouvez inspecter les ouvrages.');};
$('dismiss-intro').onclick=()=>$('map-intro').hidden=true;$('close-inspector').onclick=()=>$('asset-inspector').hidden=true;
$('collapse-results').onclick=()=>{$('results-panel').hidden=true;$('reopen-results').hidden=false;};$('reopen-results').onclick=()=>{$('results-panel').hidden=false;$('reopen-results').hidden=true;};
$('open-panel').onclick=()=>$('project-panel').classList.add('open');$('close-panel').onclick=()=>$('project-panel').classList.remove('open');
$('locate-project').onclick=()=>{if(!navigator.geolocation){toast('La géolocalisation n’est pas disponible.',true);return;}navigator.geolocation.getCurrentPosition(position=>setCoordinates(position.coords.latitude,position.coords.longitude,true),()=>toast('Position non disponible. Vous pouvez cliquer sur la carte ou saisir vos coordonnées.',true),{enableHighAccuracy:true,timeout:15000});};
mountIcons();updateSelection(true);updatePower();
try{
  state.config=await api('/api/config');
  if(!Array.isArray(state.config.connection_voltages_kv))throw new Error('Redémarrez le serveur avec python app.py pour activer la base solaire et le choix des tensions.');
  if(Object.keys(state.config.countries||{}).join(',')!=='France')throw new Error('Le serveur utilise encore l’ancienne version. Arrêtez-le avec Ctrl+C puis relancez python app.py pour activer la version France.');
  for(const id of ['start','end']){const options=[];for(let year=state.config.last_full_year;year>=(state.config.first_full_year||2022);year--){const option=node('option','',String(year));option.value=year;options.push(option);}$(id).replaceChildren(...options);}
  for(const value of state.config.connection_voltages_kv){const label=node('label','voltage-choice');const checkbox=node('input');checkbox.type='checkbox';checkbox.value=String(value);checkbox.dataset.gridVoltage='';checkbox.defaultChecked=state.config.defaults.connection_voltages_kv.includes(value);label.append(checkbox,document.createTextNode(`${number(value,1)} kV`));$('connection-voltages').append(label);}
  fillForm(state.config.defaults);updateYears();refreshSolarCache();
  $('api-status').replaceChildren(node('i'),document.createTextNode('Local · connecté'));$('api-status').title='Backend local disponible. Les fournisseurs de données nécessitent Internet.';
  const projectId=new URL(location.href).searchParams.get('project');if(projectId){try{const data=await api(`/api/projects/${projectId}`);fillForm(data.config);showResult(data);updateSelection(true);}catch(exc){toast(exc.message,true);}}
}catch(exc){$('api-status').textContent=state.config?'Redémarrage requis':'Backend indisponible';$('analyze-button').disabled=true;$('network-button').disabled=true;error(exc.message);toast(exc.message,true);}

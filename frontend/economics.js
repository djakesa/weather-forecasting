/* Economic results are computed by the backend; charts use no external service. */
const ns='http://www.w3.org/2000/svg';
const fmt=(value,digits=0)=>value==null?'—':new Intl.NumberFormat('fr-FR',{maximumFractionDigits:digits}).format(value);
const eur=value=>value==null?'—':Math.abs(value)>=1e6?fmt(value/1e6,2)+' M€':Math.abs(value)>=1000?fmt(value/1000,1)+' k€':fmt(value)+' €';
const el=(tag,cls='',text)=>{const node=document.createElement(tag);node.className=cls;if(text!==undefined)node.textContent=text;return node;};
const svg=(tag,attrs={})=>{const node=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);return node;};
const recovery=(years,horizon)=>years==null?'Non atteint sur '+horizon+' ans':fmt(years,1)+' ans';

function chartFrame(title,values,years){
  const figure=el('figure','economic-chart');figure.append(el('figcaption','',title));
  const graphic=svg('svg',{viewBox:'0 0 760 310',role:'img','aria-label':title});
  const caption=svg('title');caption.textContent=title;graphic.append(caption);
  const unit=Math.max(...values.map(Math.abs))>=1000?1000:1;
  let min=Math.min(0,...values),max=Math.max(0,...values);if(max===min)max=min+1;
  const padding=(max-min)*.08;min-=padding;max+=padding;
  const x=year=>65+year/Math.max(1,years)*675,y=value=>255-(value-min)/(max-min)*230;
  for(let i=0;i<=4;i++){const value=min+(max-min)*i/4;graphic.append(svg('line',{x1:65,x2:740,y1:y(value),y2:y(value),stroke:'#e3e9ef'}));const label=svg('text',{x:57,y:y(value)+4,'text-anchor':'end',fill:'#62788c','font-size':11});label.textContent=fmt(value/unit,0);graphic.append(label);}
  const unitLabel=svg('text',{x:65,y:15,fill:'#62788c','font-size':11});unitLabel.textContent=unit===1000?'k€':'€';graphic.append(unitLabel);
  for(let i=0;i<=5;i++){const year=Math.round(years*i/5),label=svg('text',{x:x(year),y:278,'text-anchor':'middle',fill:'#62788c','font-size':11});label.textContent='An '+year;graphic.append(label);}
  graphic.append(svg('line',{x1:65,x2:740,y1:y(0),y2:y(0),stroke:'#90a4b8','stroke-dasharray':'4 3'}));
  const zero=svg('text',{x:57,y:y(0)+4,'text-anchor':'end',fill:'#294b6a','font-size':11});zero.textContent='0';graphic.append(zero);
  figure.append(graphic);return {figure,graphic,x,y};
}

function cumulativeChart(data){
  const rows=data.central.rows,values=rows.flatMap(row=>[row.cumulative_eur,row.discounted_cumulative_eur]);
  values.push(...data.optimistic.rows.map(row=>row.cumulative_eur),...data.pessimistic.rows.map(row=>row.cumulative_eur));
  const {figure,graphic,x,y}=chartFrame('Quand le projet rembourse-t-il son investissement ?',values,data.assumptions.years);
  const band=[...data.optimistic.rows.map(row=>[x(row.year),y(row.cumulative_eur)]),...data.pessimistic.rows.toReversed().map(row=>[x(row.year),y(row.cumulative_eur)])];
  graphic.append(svg('polygon',{points:band.map(p=>p.join(',')).join(' '),fill:'#dce8fa',opacity:.7}));
  for(const [key,color] of [['cumulative_eur','#2459a6'],['discounted_cumulative_eur','#20806a']]){
    graphic.append(svg('polyline',{points:rows.map(row=>x(row.year)+','+y(row[key])).join(' '),fill:'none',stroke:color,'stroke-width':2.8}));
    for(const row of rows){const point=svg('circle',{cx:x(row.year),cy:y(row[key]),r:4,fill:color,tabindex:0});const title=svg('title');title.textContent='Année '+row.year+' · '+(key==='cumulative_eur'?'Cumul net':'Cumul actualisé')+' : '+eur(row[key]);point.append(title);graphic.append(point);}
  }
  const legend=el('div','economic-legend');legend.append(el('span','cash','Cumul net'),el('span','discounted','Cumul actualisé'),el('span','range','Plage liée au CAPEX'));figure.append(legend,el('p','field-help','Le passage au-dessus de zéro indique le remboursement estimé. Les renouvellements peuvent réduire le cumul après ce franchissement.'));
  return figure;
}

function annualChart(data){
  const rows=data.central.rows.slice(1),cost=row=>row.opex_eur+row.replacement_eur+row.decommissioning_eur;
  const {figure,graphic,x,y}=chartFrame('Recettes et dépenses, année par année',rows.flatMap(row=>[row.revenue_eur,cost(row)]),data.assumptions.years);
  const width=Math.min(9,Math.max(2,220/rows.length));
  for(const row of rows)for(const [value,color,offset,label] of [[row.revenue_eur,'#2459a6',-width,'Recettes'],[cost(row),'#bd8050',0,'Dépenses']]){
    const rect=svg('rect',{x:x(row.year)+offset,y:y(value),width,height:Math.max(0,y(0)-y(value)),fill:color,rx:1,tabindex:0});
    const title=svg('title');title.textContent='Année '+row.year+' · '+label+' : '+eur(value);rect.append(title);graphic.append(rect);
  }
  const legend=el('div','economic-legend');legend.append(el('span','cash','Recettes'),el('span','expenses','Exploitation, renouvellement et fin de vie'));figure.append(legend);return figure;
}

export function createEconomicsPage({request,navigate}){
  const page=el('section','economics-page');page.id='economics-page';page.hidden=true;page.setAttribute('aria-label','Analyse économique');document.querySelector('.workspace').append(page);
  let project=null,data=null,loadedId=null,sequence=0,sourceDirty=false;
  const compact=window.matchMedia('(max-width:850px)');compact.addEventListener('change',()=>{const controls=page.querySelector('.economics-assumptions');if(controls)controls.open=!compact.matches;});
  function header(){const heading=el('div','economics-heading'),text=el('div');text.append(el('span','eyebrow','ÉTAPE 3 · SIMULATION ÉCONOMIQUE'),el('h1','','Indicateurs économiques'),el('p','project-intro','Coût de l’énergie, revenus et temps de retour selon vos hypothèses.'));const technical=el('button','button secondary','Bilan technique');technical.onclick=()=>navigate('bilan');heading.append(text,technical);page.append(heading);const warning=el('p','warning-note economic-source-stale','Les paramètres techniques ont changé. Cette simulation reste fondée sur l’analyse enregistrée ; relancez la recherche sur la carte pour les appliquer.');warning.hidden=!sourceDirty;page.append(warning);}
  async function load(){
    if(!project||project.kind!=='analysis'){page.replaceChildren();header();page.append(el('p','warning-note','Lancez une analyse solaire pour accéder aux indicateurs économiques.'));return;}
    if(loadedId===project.id&&data){if(!page.querySelector('#economics-form'))draw();return;}
    const token=++sequence;page.replaceChildren();header();const status=el('p','economics-loading','Calcul des indicateurs économiques…');status.setAttribute('role','status');page.append(status);
    try{const result=await request('/api/projects/'+project.id+'/economics');if(token!==sequence)return;data=result;loadedId=project.id;draw();}
    catch(error){if(token!==sequence)return;loadedId=null;data=null;if(!status.isConnected){page.replaceChildren();header();}status.textContent=error.message;status.classList.add('warning-note');const retry=el('button','button secondary','Réessayer');retry.onclick=load;page.append(status,retry);}
  }
  function draw(){
    page.replaceChildren();header();if(!data)return;
    const layout=el('div','economics-layout'),assumptions=el('details','economics-assumptions'),results=el('div','economics-results');assumptions.open=!compact.matches||data.status==='incomplete';layout.append(assumptions,results);page.append(layout);
    assumptions.append(el('summary','','Vos hypothèses'),el('p','field-help','Exemples à adapter, sans garantie de prix. Le recalcul ne relance aucune donnée météo.'));
    const form=el('form','economics-form');form.id='economics-form';assumptions.append(form);
    const scenarioLabel=el('label','field','Scénario étudié'),select=el('select');select.name='scenario_index';select.setAttribute('aria-label','Scénario économique');['Potentiel solaire · hypothèse','Proximité réseau · hypothèse','Implantation parcellaire'].forEach((name,index)=>{const option=el('option','',name);option.value=index;select.append(option);});select.value=data.assumptions.scenario_index;scenarioLabel.append(select);form.append(scenarioLabel);
    function input(parent,key,label,min,max,step,help){
      const field=el('label','field'),caption=el('span','field-label-row',label),info=el('button','field-info','i');info.type='button';info.title=help;info.setAttribute('aria-label','Aide : '+label);caption.append(info);
      const note=el('span','field-tip',help);note.id='economic-help-'+key;note.hidden=true;info.setAttribute('aria-expanded','false');info.setAttribute('aria-controls',note.id);info.onclick=event=>{event.preventDefault();note.hidden=!note.hidden;info.setAttribute('aria-expanded',String(!note.hidden));};
      const control=el('input');control.type='number';control.name=key;control.min=min;control.max=max;control.step=step;control.value=data.assumptions[key]??'';control.required=key!=='capex_override_eur';control.setAttribute('aria-describedby',note.id);field.append(caption,control,note);parent.append(field);
    }
    input(form,'sale_price_eur_mwh','Prix de vente (€/MWh)',0,2000,.01,'Valeur de toute l’électricité produite, en euros constants. Prix d’exemple, sans contrat ni tarif garanti.');
    input(form,'years','Durée du projet (ans)',1,60,1,'Horizon de la simulation à compter de la mise en service.');
    input(form,'opex_eur_kwp_year','Exploitation (€/kWc/an)',0,1000,.01,'Maintenance et exploitation annuelles par kWc réellement installé. Les coûts supplémentaires se renseignent ci-dessous.');
    input(form,'discount_percent','Actualisation réelle (%)',0,30,.1,'Taux utilisé pour exprimer les flux futurs en valeur actuelle, sans inflation. Ce n’est pas un taux d’emprunt.');
    const advanced=el('details','economic-advanced');advanced.open=data.status==='incomplete';advanced.append(el('summary','','Hypothèses avancées'));form.append(advanced);
    input(advanced,'degradation_percent','Dégradation annuelle (%)',0,10,.1,'Baisse annuelle de la production à partir de la deuxième année.');
    input(advanced,'additional_opex_eur_year','Autres charges (€/an)',0,1e8,.01,'Loyer, assurance ou autres dépenses non incluses dans le coût par kWc. Évitez les doubles comptes.');
    input(advanced,'replacement_year','Année de renouvellement',1,60,1,'Année d’une dépense exceptionnelle de renouvellement. Au-delà de la durée du projet, elle n’est pas comptée.');
    input(advanced,'replacement_eur_kwp','Renouvellement (€/kWc)',0,5000,1,'Dépense ponctuelle, par exemple pour les onduleurs. Hypothèse indépendante des données réseau.');
    input(advanced,'decommissioning_eur','Fin de vie (€)',0,1e9,.01,'Dépense additionnelle comptée en dernière année. Aucune valeur de revente n’est supposée.');
    input(advanced,'capex_override_eur','Investissement manuel (€)',0,1e10,.01,'Facultatif : remplace intégralement le CAPEX estimé, par exemple par un devis complet incluant le foncier et le raccordement.');
    const submit=el('button','button primary','Recalculer et enregistrer');submit.type='submit';const state=el('p','field-help','Hypothèses conservées avec ce projet après recalcul.');state.setAttribute('role','status');form.append(submit,state);
    const exportLink=el('a','button secondary','Exporter les flux annuels · CSV');exportLink.href='/api/projects/'+project.id+'/export/economics.csv';if(data.status==='calculated')assumptions.append(exportLink);
    form.oninput=()=>{state.textContent='Hypothèses modifiées : cliquez sur Recalculer.';results.classList.add('is-stale');exportLink.setAttribute('aria-disabled','true');};exportLink.onclick=event=>{if(exportLink.getAttribute('aria-disabled')==='true')event.preventDefault();};
    form.onsubmit=async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      const parameters={};for(const control of form.elements){if(!control.name)continue;parameters[control.name]=control.value===''?null:Number(control.value);}
      const token=++sequence;for(const control of form.elements)control.disabled=true;state.textContent='Recalcul en cours…';
      try{const response=await request('/api/projects/'+project.id+'/economics',{method:'POST',body:JSON.stringify(parameters)});if(token!==sequence)return;data=response;draw();}
      catch(error){if(token!==sequence)return;state.textContent=error.message;for(const control of form.elements)control.disabled=false;}
    };
    const acquisition=data.acquisition;
    if(acquisition&&acquisition.status!=='manual'){
      const parcelScenario=data.assumptions.scenario_index===2;
      const {status,low_eur:low,high_eur:high,priced_parcels:priced,total_parcels:total}=acquisition;
      let message=status==='included'?'Acquisition foncière incluse dans le CAPEX.':
        status==='partial'?'Acquisition foncière partiellement chiffrée.':
        parcelScenario?'Acquisition foncière à chiffrer.':'Acquisition foncière à chiffrer pour ce scénario de présélection. Consultez le scénario Implantation parcellaire pour les estimations par parcelle.';
      if(total>0)message+=' '+priced+' sur '+total+' parcelles chiffrées.';
      if(status!=='missing'&&low!=null&&high!=null)message+=' Montant foncier pris en compte : '+(low===high?eur(low):eur(low)+' à '+eur(high))+'.';
      if(parcelScenario&&status!=='included'&&project.config?.price_mode==='Ventes locales comparables'){
        message+=' Consultez l’onglet Parcelles pour voir les terrains chiffrés par DVF et ceux à compléter.';
      }
      results.append(el('p',status==='included'?'economic-scope':'warning-note',message));
    }
    if(data.status!=='calculated'){results.append(el('div','warning-note',data.reason));if(data.status==='incomplete'){const manual=el('button','button secondary','Renseigner l’investissement');manual.onclick=()=>{assumptions.open=true;advanced.open=true;form.elements.namedItem('capex_override_eur').focus();};results.append(manual);}return;}
    const central=data.central,low=data.optimistic,high=data.pessimistic,horizon=data.assumptions.years;
    const scope=el('div','economic-scope');scope.append(el('span','','Investissement central : '+eur(central.capex_eur)),el('span','','Année 1 : '+fmt(data.first_year_energy_mwh,1)+' MWh'),el('span','','Puissance : '+fmt(data.installed_kwp,1)+' kWc'));results.append(scope);
    const cards=el('div','economic-kpis');
    function metric(title,value,note,cls=''){const card=el('article','economic-kpi '+cls);card.append(el('h2','',title),el('strong','',value),el('p','',note));cards.append(card);}
    metric('LCOE',fmt(central.lcoe_eur_mwh,1)+' €/MWh','Coût actualisé de l’énergie · '+fmt(low.lcoe_eur_mwh,1)+' à '+fmt(high.lcoe_eur_mwh,1)+' €/MWh');
    metric('Retour sur investissement',recovery(central.payback_years,horizon),'Actualisé : '+recovery(central.discounted_payback_years,horizon)+' · CAPEX bas / haut : '+recovery(low.payback_years,horizon)+' / '+recovery(high.payback_years,horizon));
    metric('Valeur actuelle nette',eur(central.npv_eur),'À '+fmt(data.assumptions.discount_percent,1)+' % · '+eur(high.npv_eur)+' à '+eur(low.npv_eur),central.npv_eur>=0?'positive':'negative');
    metric('Gain net sur '+horizon+' ans',eur(central.net_gain_eur),central.roi_percent==null?'Hors financement et impôts':'Rendement cumulé non annualisé : '+fmt(central.roi_percent,1)+' %');results.append(cards);
    for(const note of data.notes.filter(n=>/Acquisition|partielle|présélection|manuel|Raccordement/.test(n)))results.append(el('p','warning-note',note));
    if(central.payback_reversed||central.discounted_payback_reversed)results.append(el('p','warning-note','Un cumul net ou actualisé redevient négatif après son premier retour à zéro. Consultez les renouvellements et la fin de vie dans le graphique.'));
    results.append(cumulativeChart(data),annualChart(data));
    const sensitivity=el('section','economic-sensitivity');sensitivity.append(el('h2','','Si le prix de vente change…'));const table=el('table');const head=el('thead'),headerRow=el('tr');['Hypothèse de prix','VAN','Retour simple'].forEach(text=>{const cell=el('th','',text);cell.scope='col';headerRow.append(cell);});head.append(headerRow);table.append(head);const body=el('tbody');for(const row of data.sensitivity){const line=el('tr');[fmt(row.price_eur_mwh,1)+' €/MWh',eur(row.npv_eur),recovery(row.payback_years,horizon)].forEach(text=>line.append(el('td','',text)));body.append(line);}table.append(body);sensitivity.append(table,el('p','field-help','Sensibilité à −20 % / prix saisi / +20 %, au CAPEX central. Ce ne sont pas des prévisions de marché.'));results.append(sensitivity);
    const method=el('details','economic-method');method.append(el('summary','','Méthode et périmètre'));for(const note of data.notes)method.append(el('p','field-help',note));method.append(el('p','field-help','LCOE = coûts actualisés / énergie actualisée. Le retour simple et le retour actualisé sont interpolés entre deux fins d’année. Les plages correspondent au CAPEX bas et haut, sans varier la météo.'));const source=el('a','source-link','Méthode LCOE · NLR / NREL ↗');source.href='https://nrel.sitefinity.cloud/pv/lcoe-calculator/documentation';source.target='_blank';source.rel='noopener noreferrer';method.append(source);results.append(method);
  }
  window.addEventListener('workspace:view',event=>{if(event.detail==='economie'){const toast=document.getElementById('toast');if(toast)toast.hidden=true;load();}});
  return {setProject(value){sourceDirty=false;page.querySelectorAll('.economic-source-stale').forEach(el=>el.hidden=true);if(value?.id!==project?.id){sequence++;loadedId=null;data=null;}project=value;if(document.body.dataset.view==='economie')load();},markProjectDirty(){sourceDirty=true;page.querySelectorAll('.economic-source-stale').forEach(el=>el.hidden=false);}};
}

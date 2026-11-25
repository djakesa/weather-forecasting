/* Guided information page, then point selection and analysis on the map. */
export function setupWorkspace(onResize){
  const form=document.getElementById('project-form');
  const sections=[...form.querySelectorAll(':scope > .form-section')],settings=[...form.querySelectorAll(':scope > details')];
  const name=form.querySelector(':scope > .field'),reset=document.getElementById('reset-project');reset.hidden=true;
  const groups=document.createElement('div');groups.className='setup-groups';form.prepend(groups);
  function group(title,children,open=false){const el=document.createElement('details');el.className='setup-group';el.open=open;const summary=document.createElement('summary');summary.textContent=title;const body=document.createElement('div');body.className='setup-group-body';body.append(...children);el.append(summary,body);groups.append(el);return el;}
  const gpsFields=sections[0].querySelector('.coordinate-fields'),locate=document.getElementById('locate-project');
  const radiusFields=document.getElementById('map-radius');
  const years=sections[1].querySelector('.coordinate-fields'),periodHelp=document.getElementById('weather-period-help');
  group('Votre installation',[name,sections[1]],true);
  form.prepend(document.getElementById('country'));
  sections[0].remove();
  group('Période météo',[years,periodHelp]);
  group('Raccordement souhaité',[sections[2]]);
  settings.forEach(el=>{el.classList.add('setup-group');el.open=false;groups.append(el);});
  const network=document.getElementById('network-button');settings.at(-1).append(network);
  const footer=form.querySelector('.panel-footer'),next=document.createElement('button');next.type='button';next.id='continue-to-map';next.className='button primary';next.innerHTML='Continuer vers la carte <span aria-hidden="true">→</span>';footer.prepend(next);
  const mapSetup=document.createElement('aside');mapSetup.className='map-setup';mapSetup.id='map-setup';
  mapSetup.innerHTML='<span class="eyebrow">ÉTAPE 2 SUR 3</span><h1>Où lancer la recherche ?</h1><p>Cliquez sur la carte pour choisir le centre du cercle.</p><p id="map-project-summary" class="map-project-summary"></p>';
  mapSetup.append(locate);
  const gps=document.createElement('details');gps.className='gps-entry';const gpsSummary=document.createElement('summary');gpsSummary.textContent='Saisir un point GPS';gps.append(gpsSummary,gpsFields);mapSetup.append(gps);
  const useGps=document.createElement('button');useGps.type='button';useGps.className='button secondary';useGps.textContent='Utiliser ce point GPS';useGps.onclick=()=>{const inputs=[...gpsFields.querySelectorAll('input')];if(inputs.every(input=>input.reportValidity())){inputs[0].dispatchEvent(new Event('change',{bubbles:true}));gps.open=false;}};gps.append(useGps);
  mapSetup.append(radiusFields);
  mapSetup.querySelectorAll('input').forEach(input=>input.setAttribute('form','project-form'));
  const status=document.createElement('p');status.id='point-status';status.className='point-status';status.textContent='Choisissez un point pour continuer.';mapSetup.append(status);
  const start=document.getElementById('analyze-button');start.setAttribute('form','project-form');start.innerHTML='Lancer la recherche <span aria-hidden="true">→</span>';start.disabled=true;mapSetup.append(start);
  const back=document.createElement('button');back.type='button';back.className='text-button';back.textContent='← Modifier mes informations';back.onclick=()=>go('projet');mapSetup.append(back);
  document.querySelector('.map-workspace').append(mapSetup);
  const nav=document.createElement('nav');nav.className='workspace-nav';nav.setAttribute('aria-label','Parcours du projet');
  for(const [view,label] of [['projet','1. Informations'],['carte','2. Carte'],['economie','3. Indicateurs économiques']]){const button=document.createElement('button');button.type='button';button.dataset.view=view;button.textContent=label;button.onclick=()=>go(view);nav.append(button);}
  document.querySelector('.header-actions').prepend(nav);
  let ready=false,canChoose=false,confirmed=false,picking=false,busy=false,financialAvailable=false;
  function go(view,updateHash=true){
    if(busy&&view!=='carte')view='carte';
    if(!['projet','carte','bilan','economie'].includes(view)||(['bilan','economie'].includes(view)&&!ready)||(view==='economie'&&!financialAvailable)||(view==='carte'&&!canChoose&&!ready))view='projet';
    document.body.dataset.view=view;document.body.dataset.picking=String(picking);
    nav.querySelectorAll('button').forEach(button=>{button.classList.toggle('active',button.dataset.view===view);button.setAttribute('aria-current',button.dataset.view===view?'step':'false');button.disabled=busy||(button.dataset.view==='economie'&&(!ready||!financialAvailable))||(button.dataset.view==='carte'&&!canChoose&&!ready);});
    mapSetup.hidden=view!=='carte'||!picking||busy;
    if(updateHash&&location.hash!=='#'+view)history.pushState(null,'','#'+view);
    if(view==='bilan'){document.getElementById('results-panel').hidden=false;document.getElementById('asset-inspector').hidden=true;}
    const economicPage=document.getElementById('economics-page');if(economicPage)economicPage.hidden=view!=='economie';
    window.dispatchEvent(new CustomEvent('workspace:view',{detail:view}));
    requestAnimationFrame(()=>onResize(view,picking));
  }
  function continueToMap(){
    if(busy)return;
    const invalid=[...form.elements].find(el=>form.contains(el)&&el.willValidate&&!el.checkValidity());
    if(invalid){reveal(invalid);invalid.reportValidity();return;}
    canChoose=true;picking=true;
    const surface=Number(form.elements.namedItem('surface').value).toLocaleString('fr-FR');
    document.getElementById('map-project-summary').textContent=surface+' m² de panneaux';
    start.disabled=!confirmed;go('carte');
    if(document.getElementById('edit-location').getAttribute('aria-pressed')!=='true')document.getElementById('edit-location').click();
  }
  function reveal(input){if(mapSetup.contains(input)){picking=true;canChoose=true;go('carte');}else go('projet');let parent=input.parentElement;while(parent&&parent!==form){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}}
  next.onclick=continueToMap;
  form.addEventListener('invalid',event=>reveal(event.target),true);
  window.addEventListener('hashchange',()=>go(location.hash.slice(1),false));
  const helpText={
    name:'Nom utilisé pour retrouver cette étude dans vos projets sauvegardés.',
    surface:'Somme des surfaces des modules photovoltaïques en m². Le terrain nécessaire est plus grand et dépend de la couverture au sol.',
    radius:'Distance maximale autour du point qui sera choisi sur la carte. Un grand rayon compare des secteurs ; le cadastre reste interrogé localement.',
    'radius-value':'Distance en kilomètres autour du point choisi sur la carte.',
    lat:'Latitude du centre, en degrés décimaux. Vous pouvez aussi cliquer directement sur la carte.',
    lon:'Longitude du centre, en degrés décimaux. France métropolitaine et Corse uniquement.',
    start:'Première année complète utilisée pour calculer le potentiel solaire moyen.',
    end:'Dernière année complète utilisée. Une période plus longue lisse les différences entre années.',
    tilt:'Inclinaison des panneaux par rapport à l’horizontale, en degrés. Elle influence l’irradiance reçue.',
    pr:'Part de la production théorique conservée après les pertes du système. 0,8 signifie 80 %.',
    density:'Puissance crête des modules par m² de panneaux. Sert à convertir la surface en kWc.',
    coverage:'Rapport surface des panneaux / surface du terrain utilisé pour dimensionner les emprises. 0,4 signifie 40 %. Ce n’est pas la couverture réglementaire.',
    point_budget:'Nombre maximal de points météo à récupérer. Un budget plus élevé compare davantage de positions, avec plus de temps et de quota API.',
    weather_reuse_m:'Distance maximale pour réutiliser un point météo déjà stocké, à modèle, période et inclinaison identiques.',
    loss:'Préférence initiale de perte solaire par rapport au meilleur point. Si aucune solution complète avec réseau n’est trouvée, une seconde passe peut l’assouplir.',
    move:'Distance locale de recherche autour des points solaires et contribution au rayon de prospection du raccordement.',
    parcel_radius:'Rayon local dans lequel les parcelles sont recherchées pour chaque secteur solaire.',
    max_sectors:'Nombre de secteurs par passe. Deux passes au maximum si les premiers secteurs n’offrent pas une implantation complète avec cible réseau.',
    price_mode:'La moyenne locale DVF utilise des ventes de terrains nus de type et de surface comparables. Une référence agricole départementale ou une hypothèse manuelle sont aussi disponibles.',
    manual_price:'Hypothèse de prix d’achat du terrain, en euros par m². Ce n’est pas une valeur de marché certifiée.',
    price_uncertainty:'Marge relative autour du prix foncier retenu. 0,5 correspond à une variation de 50 %.',
    land_basis:'Le coût foncier porte sur les parcelles entières ou sur la seule emprise des panneaux.',
    detour:'Facteur appliqué à la distance directe pour estimer une longueur de travaux plus réaliste.',
    contingency:'Provision ajoutée aux coûts calculés pour couvrir les incertitudes du projet.',
    collector_voltage:'Tension de calcul du nouveau collecteur entre les champs PV, en kV.',
    dc_ac_ratio:'Rapport entre la puissance des panneaux et la puissance alternative des onduleurs.',
    power_factor:'Facteur de puissance utilisé pour le dimensionnement indicatif du raccordement.',
    transformer_margin:'Marge de dimensionnement du transformateur neuf du projet.',
    build_transformer:'Inclut dans les hypothèses de coût la création d’un transformateur de projet.',
    automatic_ratings:'Complète les sources cartographiques par des appels OSM bruts supplémentaires ; leur disponibilité peut ralentir le calcul.',
    'cadastre-file':'Cadastre officiel personnel au format GeoJSON, utilisé à la place du téléchargement automatique.',
    'inventory-file':'Inventaire électrique CSV avec identifiants exacts et provenance, pour compléter les valeurs publiées.'
  };
  let helpIndex=0;
  function enhanceHelp(){
    document.querySelectorAll('#project-form label,#map-setup label').forEach(label=>{
      if(label.dataset.helpReady)return;
      const control=label.querySelector('input,select')||document.getElementById(label.htmlFor);if(!control)return;
      label.dataset.helpReady='true';
      const key=control.name||control.id;let text=helpText[key];
      if(control.hasAttribute('data-grid-voltage'))text='Autorise une cible dont la tension publiée correspond à '+control.value+' kV. Une tension ne prouve pas une capacité d’injection disponible.';
      if(!text&&/_(low|high)$/.test(key))text='Hypothèses basse et haute de coût unitaire, dans l’unité indiquée. Le résultat reste une estimation à confirmer par devis.';
      if(!text)text='Valeur utilisée pour '+(control.getAttribute('aria-label')||label.textContent.trim().split('\n')[0])+'. Vérifiez l’unité indiquée avant de la modifier.';
      const button=document.createElement('button');button.type='button';button.className='field-info';button.textContent='i';button.setAttribute('aria-label','Aide : '+(control.name||control.id||'paramètre'));button.setAttribute('aria-expanded','false');
      const tip=document.createElement('span');tip.className='field-tip';tip.id='field-help-'+(++helpIndex);tip.setAttribute('role','tooltip');tip.textContent=text;tip.hidden=true;
      button.setAttribute('aria-controls',tip.id);control.setAttribute('aria-describedby',((control.getAttribute('aria-describedby')||'')+' '+tip.id).trim());
      button.onclick=event=>{event.preventDefault();event.stopPropagation();const open=tip.hidden;document.querySelectorAll('.field-tip').forEach(el=>el.hidden=true);document.querySelectorAll('.field-info').forEach(el=>el.setAttribute('aria-expanded','false'));tip.hidden=!open;button.setAttribute('aria-expanded',String(open));};
      button.onkeydown=event=>{if(event.key==='Escape'){tip.hidden=true;button.setAttribute('aria-expanded','false');}};
      if(control.type==='checkbox')label.append(button,tip);else{
        const texts=[...label.childNodes].filter(el=>el.nodeType===Node.TEXT_NODE&&el.textContent.trim());
        if(texts.length){const caption=document.createElement('span');caption.className='field-label-row';texts.forEach(el=>caption.append(el));caption.append(button);label.prepend(caption);}
        else if(label.firstElementChild&&!label.firstElementChild.contains(control))label.firstElementChild.append(button);
        else label.insertBefore(button,label.firstElementChild);
        label.append(tip);
      }
    });
  }
  enhanceHelp();new MutationObserver(enhanceHelp).observe(document.querySelector('.workspace'),{childList:true,subtree:true});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){document.querySelectorAll('.field-tip').forEach(el=>el.hidden=true);document.querySelectorAll('.field-info').forEach(el=>el.setAttribute('aria-expanded','false'));}});
  const progress=document.createElement('div');progress.id='analysis-steps';progress.className='analysis-steps';
  for(const [key,label] of [['solar','Irradiance'],['sites','Parcelles et réseau'],['render','Carte et résultats']]){const row=document.createElement('div');row.dataset.progressStep=key;const title=document.createElement('span');title.textContent=label;const bar=document.createElement('progress');bar.max=1;bar.value=0;bar.setAttribute('aria-label',label);row.append(title,bar);progress.append(row);}
  document.querySelector('#progress-card .progress-track').replaceWith(progress);
  const count=document.createElement('p');count.id='progress-count';count.className='progress-count';document.getElementById('progress-message').after(count);
  document.getElementById('export-button').hidden=true;go(location.hash.slice(1)||'projet',false);
  return {
    go,continue:continueToMap,step(){groups.firstElementChild.open=true;},
    confirmPoint(){confirmed=true;status.textContent='Point sélectionné. Vous pouvez lancer la recherche.';status.classList.add('confirmed');start.disabled=busy;},
    canRun(){return confirmed;},
    busy(value){busy=value;document.body.dataset.busy=String(value);next.disabled=value;start.disabled=value||!confirmed;go(document.body.dataset.view,false);},
    ready(result){ready=true;financialAvailable=result?.kind==='analysis';canChoose=true;confirmed=true;picking=false;status.textContent='Point du projet sélectionné.';status.classList.add('confirmed');reset.hidden=false;document.getElementById('export-button').hidden=false;go(['#bilan','#economie'].includes(location.hash)?location.hash.slice(1):'carte');},
    reset(){ready=false;financialAvailable=false;canChoose=false;confirmed=false;picking=false;reset.hidden=true;start.disabled=true;status.textContent='Choisissez un point pour continuer.';status.classList.remove('confirmed');gps.open=false;document.getElementById('export-button').hidden=true;go('projet');}
  };
}

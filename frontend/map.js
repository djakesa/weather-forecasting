export const CONGESTION_ZONES = {
  SATUREE: ['Saturée', '#c1273e'],
  EN_TENSION: ['En tension', '#dd7921'],
  FAVORABLE: ['Favorable', '#68932d'],
  TRES_FAVORABLE: ['Très favorable', '#147c66'],
  ELD: ['Hors périmètre Enedis', '#687584'],
};

const isOverhead = feature => ['line','minor_line'].includes(feature.properties.kind) &&
  ['LineString','MultiLineString'].includes(feature.geometry?.type);

export function retainedCadastre(data){
  const references=new Set();
  const add=reference=>{if(reference!=null)references.add(String(reference));};
  for(const row of data.parcels||[])add(row.reference_cadastrale);
  for(const {properties} of data.pv?.features||[]){
    add(properties?.idu ?? properties?.id);
  }
  return (data.cadastre?.features||[]).filter(feature=>{
    const reference=feature.properties?.idu ?? feature.properties?.id ?? feature.id;
    return reference!=null&&references.has(String(reference));
  });
}

export class ProjectMap{
  constructor(onSelect,onAsset){
    this.onSelect=onSelect;this.onAsset=onAsset;this.editing=true;
    this.map=L.map('map',{zoomControl:false,attributionControl:true,preferCanvas:true}).setView([47,6],5);
    this.basemaps=[L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}),L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&FORMAT=image/jpeg&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',{maxZoom:19,attribution:'© IGN · Géoplateforme'})];
    this.basemap=0;this.basemaps[0].addTo(this.map);
    this.selection=L.featureGroup().addTo(this.map);this.zone=L.featureGroup().addTo(this.map);
    this.map.createPane('irradiance').style.zIndex=390;
    this.map.createPane('congestion').style.zIndex=395;
    this.layers={congestion:L.layerGroup(),solar:L.layerGroup().addTo(this.map),network:L.layerGroup(),pv:L.layerGroup(),constraints:L.layerGroup()};
    this.visible={solar:true,network:false,pv:false};
    this.map.on('click',e=>{if(this.editing)this.onSelect(e.latlng.lat,e.latlng.lng);});
    this.map.on('zoomend',()=>this.updateLabels());
    this.map.on('mousemove',e=>{document.getElementById('map').style.cursor=this.editing?'crosshair':'';});
    new ResizeObserver(()=>this.map.invalidateSize({pan:false})).observe(document.getElementById('map'));
  }
  setSelection(lat,lon,radius,fit=false){
    this.selection.clearLayers();this.center=[lat,lon];this.radius=radius;
    this.circle=L.circle(this.center,{radius:radius*1000,color:'#2459a6',weight:1.8,fillColor:'#bfd3f0',fillOpacity:.07,interactive:false,dashArray:'5 5'}).addTo(this.selection);
    L.circleMarker(this.center,{radius:5,color:'#fff',weight:2,fillColor:'#2459a6',fillOpacity:1,interactive:false}).addTo(this.selection);
    if(fit)this.fit();
  }
  fit(){if(this.circle)this.map.fitBounds(this.circle.getBounds(),{padding:[70,70],maxZoom:15,animate:true});}
  toggle(name,visible){this.visible[name]=visible;if(visible)this.layers[name].addTo(this.map);else this.layers[name].remove();}
  switchBasemap(){this.basemaps[this.basemap].remove();this.basemap=1-this.basemap;this.basemaps[this.basemap].addTo(this.map);}
  setEditing(value){this.editing=value;document.getElementById('edit-location').setAttribute('aria-pressed',String(value));}
  clear(){Object.values(this.layers).forEach(layer=>layer.clearLayers());this.zone.clearLayers();if(this.scenarioMarker)this.scenarioMarker.remove();this.scenarioMarker=null;this.data=null;this.featureLayers=new Map();}
  setData(data){
    this.clear();this.data=data;this.featureLayers=new Map();
    if(data.zone)L.geoJSON(data.zone,{style:{color:'#869471',weight:1,fillOpacity:0,dashArray:'4 5'},interactive:false}).addTo(this.zone);
    if(data.raster)L.imageOverlay(data.raster.url,data.raster.bounds,{pane:'irradiance',opacity:.74,interactive:false}).addTo(this.layers.solar);
    if(data.samples)L.geoJSON(data.samples,{pointToLayer:(_,latlng)=>L.circleMarker(latlng,{pane:'irradiance',radius:2.5,color:'#8b6f38',weight:.5,fillColor:'#fff8d8',fillOpacity:.8}),onEachFeature:(feature,layer)=>{const value=feature.properties.irradiance_mean_w_m2;const label=document.createElement('span');label.textContent=value==null?'Point météo échantillonné':`GTI moyenne : ${value.toFixed(1)} W/m²`;if(feature.properties.weather_reuse_distance_m>.02)label.append(document.createTextNode(" · réutilisé à "+Math.round(feature.properties.weather_reuse_distance_m)+" m"));layer.bindTooltip(label);}}).addTo(this.layers.solar);
    if(data.network){
      L.geoJSON(data.network,{
        filter:isOverhead,interactive:false,
        style:f=>({color:'#fff',weight:this.networkStyle(f.properties).weight+2,opacity:.9}),
      }).addTo(this.layers.network);
      L.geoJSON(data.network,{style:f=>this.networkStyle(f.properties),pointToLayer:(f,latlng)=>L.circleMarker(latlng,{radius:f.properties.kind==='transformer'?5:6,color:'#fff',weight:1.8,fillColor:'#8791b3',fillOpacity:(f.properties.voltage_selection_compatible===false||f.properties.planning_eligible===false) ? .3 : .95}),onEachFeature:(feature,layer)=>{
      const label=document.createElement('span');label.textContent=(feature.properties.name||feature.properties.id)+' · '+(feature.properties.voltage_label||'Tension non publiée');layer.bindTooltip(label,{direction:'top'});
      layer.on('click',e=>{L.DomEvent.stopPropagation(e);this.onAsset(feature);});this.featureLayers.set(feature.properties.id,layer);
      }}).addTo(this.layers.network);
    }
    const parcels=retainedCadastre(data);
    if(parcels.length)L.geoJSON(parcels,{style:{color:'#527349',weight:1.4,fillOpacity:.03,dashArray:'5 3'},interactive:false}).addTo(this.layers.pv);
    if(data.regulatory?.features?.length)L.geoJSON({type:'FeatureCollection',features:data.regulatory.features},{style:{color:'#976b97',weight:1.5,fillOpacity:.18,dashArray:'4 3'},onEachFeature:(feature,layer)=>{const text=document.createElement('span');text.textContent=`${feature.properties.constraint||'Contrainte'} · ${feature.properties.SITENAME||feature.properties.libelle||'consulter le règlement applicable'}`;layer.bindTooltip(text);}}).addTo(this.layers.constraints);
    if(data.pv)L.geoJSON(data.pv,{style:{color:'#51794d',weight:1.8,fillColor:'#769a5c',fillOpacity:.36},onEachFeature:(feature,layer)=>{const label=document.createElement('span');label.textContent=`Champ ${feature.properties.champ_id||feature.properties.idu||''} · ${Math.round(feature.properties.pv_m2||0).toLocaleString('fr-FR')} m² de modules`;layer.bindTooltip(label);}}).addTo(this.layers.pv);
    if(data.routes)L.geoJSON(data.routes,{style:f=>({color:f.properties.segment_type==='collecteur'?'#557e78':'#be8c4e',weight:3,opacity:.95,dashArray:f.properties.segment_type==='collecteur'?null:'6 5'}),onEachFeature:(feature,layer)=>{const label=document.createElement('span');label.textContent=feature.properties.segment_type==='collecteur'?'Collecteur entre champs':'Raccordement externe unique';layer.bindTooltip(label);}}).addTo(this.layers.pv);
    if(data.grid_constraints?.features?.length)L.geoJSON({type:'FeatureCollection',features:data.grid_constraints.features},{pane:'congestion',style:f=>({color:CONGESTION_ZONES[f.properties.status]?.[1]||'#687584',weight:1.5,fillOpacity:.29}),onEachFeature:(feature,layer)=>{const label=document.createElement('span');label.textContent=`Enedis · ${feature.properties.label} · capacité à confirmer`;layer.bindTooltip(label);}}).addTo(this.layers.congestion);
    this.pvLabels=L.layerGroup().addTo(this.layers.pv);this.voltageLabels=L.layerGroup().addTo(this.layers.network);this.updateLabels();
    const external=data.routes?.features?.find(f=>f.properties.segment_type==='raccordement_unique');
    if(external?.geometry.type==='LineString'){const xy=external.geometry.coordinates.at(-1);const label=document.createElement('span');label.textContent='Point de raccordement · '+(data.scenarios?.[2]?.connection?.voltage_label||'Voir la fiche réseau');L.circleMarker([xy[1],xy[0]],{radius:8,color:'#fff',weight:3,fillColor:'#e59937',fillOpacity:1}).bindTooltip(label).addTo(this.layers.pv);}
    this.setEditing(false);
  }
  updateLabels(){
    if(!this.data||!this.pvLabels||!this.voltageLabels)return;
    this.pvLabels.clearLayers();this.voltageLabels.clearLayers();
    const place=(feature,text,className,group)=>{const bounds=L.geoJSON(feature).getBounds();if(!bounds.isValid())return;const label=document.createElement('span');label.textContent=text;L.marker(bounds.getCenter(),{interactive:false,icon:L.divIcon({className,html:label,iconSize:null})}).addTo(group);};
    if(this.map.getZoom()>=9)(this.data.pv?.features||[]).slice(0,24).forEach((f,i)=>place(f,'PV '+(i+1),'pv-map-label',this.pvLabels));
    if(this.map.getZoom()>=12)(this.data.network?.features||[]).filter(f=>['line','minor_line','cable'].includes(f.properties.kind)&&f.properties.voltage_label?.includes('kV')).slice(0,18).forEach(f=>place(f,f.properties.voltage_label,'map-voltage-label',this.voltageLabels));
  }
  networkStyle(properties){
    const compatible=properties.voltage_selection_compatible!==false&&properties.planning_eligible!==false;
    if(['line','minor_line'].includes(properties.kind)){
      const voltages=properties.voltage_values_v?.length?properties.voltage_values_v:[Number(properties.voltage)||0];
      const maximum=Math.max(...voltages);
      return {color:maximum>=220000?'#843631':maximum>=63000?'#5d348b':'#174983',
        weight:compatible?4:3,opacity:compatible?1:.62};
    }
    return {color:'#6d82a1',weight:properties.kind==='substation'?1.5:2,opacity:compatible ? .85 : .45,
      dashArray:properties.kind==='cable'?'5 4':null,fillColor:'#a6b0ca',fillOpacity:.17};
  }
  inspect(feature){const layer=this.featureLayers.get(feature.properties.id);if(layer){const bounds=layer.getBounds?layer.getBounds():L.latLngBounds([layer.getLatLng()]);this.map.flyToBounds(bounds.pad(.3),{maxZoom:16,padding:[100,120]});this.onAsset(feature);}}
  focusScenario(scenario){if(this.scenarioMarker)this.scenarioMarker.remove();if(scenario?.location){this.scenarioMarker=L.circleMarker(scenario.location,{radius:9,color:'#fff',weight:3,fillColor:'#9b9d53',fillOpacity:1}).addTo(this.map);this.map.panTo(scenario.location);}}
}

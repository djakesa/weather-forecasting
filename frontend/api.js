export async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  let data;
  try{data=await response.json();}catch{throw new Error('Réponse du serveur illisible. Vérifiez que le backend est lancé.');}
  if(!response.ok){const detail=data.detail;throw new Error(Array.isArray(detail)?detail.map(d=>`${d.loc?.slice(1).join('.')||'Projet'} : ${d.msg}`).join('\n'):detail||`Erreur du serveur (${response.status}).`);}
  return data;
}
export async function waitForJob(id,onUpdate,signal){
  while(!signal?.aborted){
    const status=await api(`/api/jobs/${id}`);
    onUpdate(status);
    if(status.status==='done')return api(status.result_url);
    if(status.status==='failed')throw new Error(status.error||status.message);
    if(status.status==='cancelled')throw new Error(status.message);
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw new Error('Suivi de l’analyse interrompu.');
}

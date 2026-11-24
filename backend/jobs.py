"""One local analysis at a time, cooperative cancellation, disk-backed results."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from .analysis import analyze
from .adapters.network import network_snapshot
from .serialization import serialize_result

from .adapters.project_files import PROJECTS, ProjectFiles, france_snapshot


class Cancelled(Exception):
    pass


class JobManager:
    def __init__(self, directory=None, *, projects=None, analysis=None, network=network_snapshot, serialize=serialize_result):
        self.projects = projects if projects is not None else ProjectFiles(directory or PROJECTS)
        self.directory = self.projects.directory
        self.analysis = analysis
        self.network = network
        self.serialize = serialize
        self.lock=threading.RLock()
        self.jobs={}
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="solar-analysis")

    def start(self,kind,config):
        with self.lock:
            if any(j["status"] in {"queued","running","cancelling"} for j in self.jobs.values()):
                raise RuntimeError("Une analyse est déjà en cours. Attendez sa fin ou annulez-la.")
            identifier=uuid.uuid4().hex
            job=dict(id=identifier,kind=kind,status="queued",phase="Préparation",message="Préparation du projet…",logs=[],cancel=threading.Event())
            labels=[('solar','Irradiance'),('sites','Parcelles et réseau'),('render','Carte et résultats')]
            if kind=='network':labels=labels[1:]
            job['progress']=dict(completed_steps=0,total_steps=len(labels),steps=[dict(key=k,label=v,state='pending') for k,v in labels])
            self.jobs[identifier]=job
            self.executor.submit(self._run,identifier,kind,config)
            return identifier

    def progress(self,identifier,message):
        with self.lock:
            job=self.jobs[identifier]
            if job["cancel"].is_set():
                raise Cancelled()
            job["message"]=message
            stage='solar' if message.startswith('Étape 1/3') else 'sites' if message.startswith('Étape 2/3') or 'Lecture du réseau' in message else 'render' if message.startswith('Étape 3/3') else None
            steps=job['progress']['steps']
            if stage:
                job['phase']={'solar':'Météo','sites':'Parcelles et réseau','render':'Carte'}[stage]
                index=next((i for i,s in enumerate(steps) if s['key']==stage),None)
                active=max((i for i,s in enumerate(steps) if s['state']!='pending'),default=-1)
                if index is not None and index>=active:
                    for i,step in enumerate(steps):step['state']='done' if i<index else 'running' if i==index else 'pending'
            if message=='Résultats prêts':
                for step in steps:step['state']='done'
            job['progress']['completed_steps']=sum(s['state']=='done' for s in steps)
            if "Recherche solaire" in message:
                job["phase"]="Météo"
            elif "Lecture du réseau" in message:
                job["phase"]="Réseau"
            elif "Lecture des parcelles" in message or message.startswith('Secteur '):
                job["phase"]="Parcelles"
            elif message.startswith('Contraintes'):
                job['phase']='Contraintes'
            job["logs"]=(job["logs"]+[dict(time=datetime.now(timezone.utc).isoformat(),message=message)])[-200:]

    def _run(self,identifier,kind,config):
        with self.lock:
            self.jobs[identifier]["status"]="running"
        try:
            progress=lambda message:self.progress(identifier,message)
            if kind=="analysis":
                result = (self.analysis or analyze)(config, progress)
                progress("Préparation de la carte et des résultats…")
                progress("Étape 3/3 · Rendu de la carte et des résultats")
                payload = self.serialize(result, config, identifier, self.projects.artifact_directory(identifier))
            else:
                payload = self.network(config, progress, identifier)
            progress("Résultats prêts")
            with self.lock:
                payload['activity']=list(self.jobs[identifier]['logs'])
            self.projects.save(identifier, payload)
            with self.lock:
                self.jobs[identifier].update(status="done",phase="Terminé",result_url=f"/api/projects/{identifier}")
        except Cancelled:
            with self.lock:
                self.jobs[identifier].update(status="cancelled",message="Analyse annulée. Les segments de données complets restent en cache.")
        except Exception as exc:
            with self.lock:
                self.jobs[identifier].update(status="failed",message=str(exc),error=str(exc))

    def status(self,identifier):
        with self.lock:
            if identifier not in self.jobs:
                return None
            data={k:v for k,v in self.jobs[identifier].items() if k!="cancel"}
            data['progress']={**data['progress'],'steps':[dict(step) for step in data['progress']['steps']]}
            return data

    def cancel(self,identifier):
        with self.lock:
            job=self.jobs.get(identifier)
            if job is None:
                return False
            if job["status"] in {"queued","running"}:
                job["cancel"].set()
                job.update(status="cancelling",message="Annulation demandée ; la requête en cours doit se terminer.")
            return True

    def load(self, identifier):
        return self.projects.load(identifier)

    def history(self):
        return self.projects.history()

    def save_economics(self, identifier, parameters):
        with self.lock:
            self.projects.save_economics(identifier, parameters)

    def close(self):
        with self.lock:
            for identifier in self.jobs:
                self.cancel(identifier)
            self.executor.shutdown(wait=False, cancel_futures=True)

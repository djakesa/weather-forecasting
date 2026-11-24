# Atlas solaire · France

Application locale de présélection photovoltaïque pour la **France métropolitaine et la Corse** : irradiance, cadastre, obstacles, réseau électrique, coût indicatif et cadre réglementaire français.

## Lancer

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Ouvrir **http://127.0.0.1:8000**. Autre lancement : `./start-local.ps1`. Arrêt : Ctrl+C.

Si une instance tourne déjà, le programme affiche son adresse. Si un autre service occupe le port :

```powershell
.\.venv\Scripts\python.exe app.py --port 8001
```

Ouvrir alors **http://127.0.0.1:8001**. Après une mise à jour, redémarrer le serveur puis actualiser la page avec Ctrl+F5.

## Utiliser

**Page 1 · Informations** : formulaire sans carte, en sections dépliables, avec aides « i ». **Page 2 · Carte** : choix du point, recherche et couches de résultats. **Page 3 · Économie** : LCOE, retours simple et actualisé, VAN, gain net et graphiques annuels. Les hypothèses économiques se modifient et s'enregistrent par projet sans relancer la météo. Les valeurs initiales sont des exemples à adapter ; aucun tarif de vente n'est garanti. Export CSV des flux disponible. Méthode et périmètre dans [FRANCE.md](FRANCE.md#page-économie).

1. Renseigner la surface, le rayon (**0,1 à 1 000 km**), la période et les besoins de raccordement dans le formulaire.
2. Choisir le régime du projet : agrivoltaïsme ou PV au sol dans un périmètre officiel documenté, puis **Continuer vers la carte**. Poser un point en France ou confirmer un point GPS. Les données agricoles ne constituent pas une autorisation.
3. Lancer l'analyse : jusqu'à 32 points météo, puis vérification des secteurs du meilleur potentiel solaire vers les suivants. Dès qu'une surface complète et une cible réseau locale sont identifiées avec les sources requises disponibles, les recherches s'arrêtent. Sinon, jusqu'à deux passes de 6 secteurs par défaut sont comparées, sans nouvelle météo pour la seconde passe. L'écart solaire est affiché. Le rayon national n'entraîne pas un téléchargement national du cadastre ou du réseau.
4. Consulter les couches solaires, réseau, parcelles et contraintes, les caractéristiques des ouvrages et les estimations de CAPEX. L'onglet Contraintes expose les règles appliquées, les sources et les validations manquantes.
5. Exporter les géométries et le rapport, retrouver les projets français dans l'historique, ou recommencer avec les mêmes caches.

Le backend **FastAPI** et le frontend HTML/CSS/JavaScript sont servis à la même adresse, sans compilation Node. Leaflet est fourni localement ; les fonds cartographiques et les nouvelles données nécessitent Internet. Le serveur écoute uniquement sur la machine locale. API : `/docs`.

## Sources et portée

Le potentiel solaire est conservé en **SQLite** dans `data/france_cache/solar.sqlite3`, par modèle, période et inclinaison. L'interface affiche la taille du catalogue et les points réutilisés. Un point voisin peut être repris dans une distance réglable, signalée comme estimation ; choisir « Point exact uniquement » pour désactiver cette réutilisation de proximité. Les paramètres PV et financiers sont recalculés sans nouvelle récupération météo.

Le panneau **Raccordement souhaité** propose les tensions à cocher, avec 15/20/30 kV par défaut. Le filtre est respecté jusqu'à la connexion commune finale. Les valeurs non publiées ne sont pas inventées et les autres ouvrages restent visibles. Un raccordement HTB demande un chiffrage spécifique.

**Enedis** complète les localisations des lignes HTA et postes HTA/BT. Les zones indicatives de contrainte de production sont mises en cache SQLite et affichées dans la couche Réseau électrique : les zones saturées sont évitées pour une recherche HTA/BT. La source, la date de version et les limites sont visibles dans les résultats. Une copie publique ADEME / Mutafriches sert de secours au téléchargement direct. Ces données ne garantissent ni une puissance disponible ni les caractéristiques nominales de chaque transformateur. Détails dans [FRANCE.md](FRANCE.md#contraintes-de-production-enedis).

Pour importer les agrégats de vos projets français existants sans appel réseau :

```powershell
.\.venv\Scripts\python.exe tools/import_solar_cache.py
```

IGN/DGFiP pour le cadastre, IGN BD TOPO pour les obstacles, RPG pour l'usage agricole, GPU pour les zonages, servitudes et prescriptions, API Carto Nature et Natura 2000 pour les périmètres environnementaux. Réseau : Open Infrastructure Map, compléments Enedis/IGN et RTE par référence exacte. Météo : Open-Meteo, AROME depuis 2025 et GFS pour une période antérieure.

Les règles françaises sont tracées dans `france_rules.py`. Le calcul distingue règles applicables, déclarations et politiques prudentes d'évitement. Il ne délivre pas une autorisation et ne certifie pas les droits fonciers, le maintien de la production agricole ou la capacité d'injection. Voir **[FRANCE.md](FRANCE.md)** pour les références légales, le format des imports et les limites.

Les résultats restent dans `data/projects/`, les caches dans `data/france_cache/`. Les imports doivent être fournis à nouveau pour recalculer un projet qui en dépend. Les données historiques ne sont pas supprimées ; seuls les projets français sont présentés.

## Vérifications

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -q
```

Serveur lancé, Microsoft Edge installé :

```powershell
.\.venv\Scripts\python.exe tools/verify_ui.py
```

Architecture : `backend/`, `frontend/`, `france_pv.py`, `countries.py` (adaptateurs français), `regulatory.py`, `france_rules.py`, `openinfra_network.py`, `electrical_data.py`, `osm_sources.py`, `transmission_sources.py` (RTE), `land_prices.py`, `shared_grid.py`, `field_routing.py`, `spatial_sampling.py`, `map_ui.py`.

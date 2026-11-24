"""Populate SQLite from existing French snapshots without any network request."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

if __name__=='__main__':
    import france_pv as core
    from solar_store import SolarStore,import_projects
    print(json.dumps(import_projects(SolarStore(core.CACHE/'solar.sqlite3'),ROOT/'data'/'projects'),ensure_ascii=True))

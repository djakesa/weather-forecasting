"""Filesystem adapter for new and historical projects."""
import json
from pathlib import Path

from backend.economics import EconomicsInput

PROJECTS = Path(__file__).resolve().parents[2] / "data" / "projects"


class ProjectFiles:
    def __init__(self, directory=PROJECTS):
        self.directory = Path(directory)

    def artifact_directory(self, identifier):
        return self.directory / identifier

    def save(self, identifier, payload):
        directory = self.artifact_directory(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "result.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        temporary.replace(directory / "result.json")

    def raster(self, identifier):
        path = self.artifact_directory(identifier) / "irradiance.png"
        return path if path.is_file() else None

    def economics(self, identifier):
        path = self.artifact_directory(identifier) / "economics.json"
        try:
            return EconomicsInput.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return EconomicsInput()

    def save_economics(self, identifier, parameters):
        path = self.artifact_directory(identifier) / "economics.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(parameters.model_dump_json(), encoding="utf-8")
        temporary.replace(path)

    def load(self,identifier):
        path=self.directory/identifier/"result.json"
        if not path.exists():
            return None
        return france_snapshot(json.loads(path.read_text(encoding="utf-8")))

    def history(self):
        if not self.directory.exists():
            return []
        results=[]
        for path in sorted(self.directory.glob("*/result.json"),key=lambda p:p.stat().st_mtime,reverse=True)[:30]:
            try:
                data=json.loads(path.read_text(encoding="utf-8"))
                data=france_snapshot(data)
                if data is None:continue
                results.append({k:data.get(k) for k in ("id","name","kind","created_at","summary","config")})
            except (ValueError,OSError):
                continue
        return results


def france_snapshot(data):
    """Keep French legacy results readable, without exposing foreign projects."""
    country=(data.get('config') or {}).get('country')
    if country!='France' and (data.get('summary') or {}).get('terrain_country')!='France':
        return None
    data['config']={**(data.get('config') or {}),'country':'France'}
    return data

from __future__ import annotations
import json
from dataclasses import asdict
from pathlib import Path
from app.core.verification_engine import ProofPackV3

class ProofPackStore:
    def __init__(self, root: Path) -> None: self.root=root; root.mkdir(parents=True, exist_ok=True)
    def save(self, pack: ProofPackV3) -> Path:
        path=(self.root / f"{pack.run_id}.json").resolve()
        if path.parent != self.root.resolve(): raise ValueError("Invalid proof pack path")
        path.write_text(json.dumps(asdict(pack), default=str), encoding="utf-8"); return path
    def load(self, run_id: str) -> dict:
        if Path(run_id).name != run_id: raise ValueError("Invalid run ID")
        return json.loads((self.root / f"{run_id}.json").read_text(encoding="utf-8"))

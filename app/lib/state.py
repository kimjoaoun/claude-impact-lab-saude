from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DIA_DIR = Path(__file__).resolve().parent.parent / "data" / "dia"


def _prefix(equipe_id: str, cluster_id: int) -> str:
    return f"{equipe_id[:12]}_{cluster_id}_"


def snapshot_path(equipe_id: str, cluster_id: int, dia: date) -> Path:
    return DIA_DIR / f"{_prefix(equipe_id, cluster_id)}{dia.isoformat()}.json"


def load_snapshot(equipe_id: str, cluster_id: int, dia: date) -> dict | None:
    p = snapshot_path(equipe_id, cluster_id, dia)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def latest_prior_snapshot(equipe_id: str, cluster_id: int, before: date) -> dict | None:
    """Snapshot mais recente do par (equipe, cluster) com data < `before`."""
    if not DIA_DIR.exists():
        return None
    prefix = _prefix(equipe_id, cluster_id)
    candidatos: list[tuple[date, Path]] = []
    for p in DIA_DIR.glob(f"{prefix}*.json"):
        stem_date = p.stem[len(prefix):]
        try:
            d = date.fromisoformat(stem_date)
        except ValueError:
            continue
        if d < before:
            candidatos.append((d, p))
    if not candidatos:
        return None
    candidatos.sort(key=lambda t: t[0], reverse=True)
    return json.loads(candidatos[0][1].read_text())


def save_snapshot(payload: dict, equipe_id: str, cluster_id: int, dia: date) -> Path:
    DIA_DIR.mkdir(parents=True, exist_ok=True)
    out = snapshot_path(equipe_id, cluster_id, dia)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out

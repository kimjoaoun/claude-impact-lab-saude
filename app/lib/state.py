from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DIA_DIR = Path(__file__).resolve().parent.parent / "data" / "dia"


def _legacy_prefix(equipe_id: str, cluster_id: int) -> str:
    return f"{equipe_id[:12]}_{cluster_id}_"


def _prefix(equipe_id: str, cluster_id: int, panel_key: str) -> str:
    return f"{equipe_id[:12]}_{cluster_id}_{panel_key}_"


def snapshot_path(equipe_id: str, cluster_id: int, panel_key: str, dia: date) -> Path:
    return DIA_DIR / f"{_prefix(equipe_id, cluster_id, panel_key)}{dia.isoformat()}.json"


def legacy_snapshot_path(equipe_id: str, cluster_id: int, dia: date) -> Path:
    return DIA_DIR / f"{_legacy_prefix(equipe_id, cluster_id)}{dia.isoformat()}.json"


def load_snapshot(equipe_id: str, cluster_id: int, panel_key: str, dia: date) -> dict | None:
    p = snapshot_path(equipe_id, cluster_id, panel_key, dia)
    if p.exists():
        return json.loads(p.read_text())

    legacy = legacy_snapshot_path(equipe_id, cluster_id, dia)
    if legacy.exists():
        payload = json.loads(legacy.read_text())
        payload["_legacy_path"] = str(legacy)
        return payload
    return None


def latest_prior_snapshot(
    equipe_id: str,
    cluster_id: int,
    panel_key: str,
    before: date,
) -> dict | None:
    """Snapshot mais recente do mesmo painel com data < `before`."""
    if not DIA_DIR.exists():
        return None
    prefix = _prefix(equipe_id, cluster_id, panel_key)
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


def snapshots_in_range(
    equipe_id: str,
    cluster_id: int,
    panel_key: str,
    start: date,
    end: date,
) -> list[dict]:
    """Snapshots do mesmo painel no intervalo fechado [start, end]."""
    if not DIA_DIR.exists():
        return []
    prefix = _prefix(equipe_id, cluster_id, panel_key)
    candidatos: list[tuple[date, Path]] = []
    for p in DIA_DIR.glob(f"{prefix}*.json"):
        stem_date = p.stem[len(prefix):]
        try:
            d = date.fromisoformat(stem_date)
        except ValueError:
            continue
        if start <= d <= end:
            candidatos.append((d, p))
    candidatos.sort(key=lambda t: t[0])
    return [json.loads(path.read_text()) for _, path in candidatos]


def save_snapshot(
    payload: dict,
    equipe_id: str,
    cluster_id: int,
    panel_key: str,
    dia: date,
) -> Path:
    DIA_DIR.mkdir(parents=True, exist_ok=True)
    out = snapshot_path(equipe_id, cluster_id, panel_key, dia)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out

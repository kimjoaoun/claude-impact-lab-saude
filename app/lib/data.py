from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import streamlit as st
from sklearn.cluster import KMeans

BBOX = {"lon_min": -43.310, "lat_min": -22.995, "lon_max": -43.180, "lat_max": -22.885}
DATA_DIR = Path.home() / "Documents"
RANDOM_STATE = 42
MIN_PACIENTES_POR_CLUSTER = 20


@st.cache_data(show_spinner=False)
def load_equipes() -> pd.DataFrame:
    return duckdb.sql(f"""
        SELECT equipe_id,
               endereco_latitude  AS ubs_lat,
               endereco_longitude AS ubs_lon
        FROM read_parquet('{DATA_DIR}/equipes_anonimizadas.parquet')
    """).df()


@st.cache_data(show_spinner=False)
def load_visitas_agg() -> pd.DataFrame:
    """Agregado por paciente: nº de visitas + última data."""
    return duckdb.sql(f"""
        SELECT paciente_id,
               COUNT(*) AS n_visitas,
               MAX(registrados_em) AS ultima_visita
        FROM read_parquet('{DATA_DIR}/visitas_anonimizadas.parquet')
        GROUP BY paciente_id
    """).df()


@st.cache_data(show_spinner=False)
def load_eventos_agg() -> pd.DataFrame:
    """Agregado por paciente: nº de eventos + tipos distintos + último."""
    return duckdb.sql(f"""
        SELECT paciente_id,
               COUNT(*) AS n_eventos,
               COUNT(DISTINCT tipo) AS n_tipos,
               MAX(data_referencia) AS ultimo_evento,
               STRING_AGG(DISTINCT tipo, ', ') AS tipos
        FROM read_parquet('{DATA_DIR}/eventos_clinicos_anonimizados.parquet')
        GROUP BY paciente_id
    """).df()


def paciente_detalhes(
    row: pd.Series,
    visitas_agg: pd.DataFrame,
    eventos_agg: pd.DataFrame,
) -> dict:
    """Junta dados do paciente + agregados de visitas/eventos."""
    pid = row["paciente_id"]
    v = visitas_agg[visitas_agg["paciente_id"] == pid]
    e = eventos_agg[eventos_agg["paciente_id"] == pid]
    return {
        "paciente_id": pid,
        "sexo": row.get("sexo"),
        "faixa_etaria": row.get("faixa_etaria"),
        "vulnerabilidade": row.get("situacao_vulnerabilidade"),
        "hipertenso": bool(row.get("hipertenso", False)),
        "diabetico": bool(row.get("diabetico", False)),
        "gestacao": bool(row.get("gestacao", False)),
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "n_visitas": int(v["n_visitas"].iloc[0]) if not v.empty else 0,
        "ultima_visita": str(v["ultima_visita"].iloc[0]) if not v.empty else None,
        "n_eventos": int(e["n_eventos"].iloc[0]) if not e.empty else 0,
        "ultimo_evento": str(e["ultimo_evento"].iloc[0]) if not e.empty else None,
        "tipos_eventos": str(e["tipos"].iloc[0]) if not e.empty else None,
    }


@st.cache_data(show_spinner=False)
def load_pacientes() -> pd.DataFrame:
    df = duckdb.sql(f"""
        SELECT paciente_id, equipe_id,
               endereco_latitude  AS lat,
               endereco_longitude AS lon,
               faixa_etaria, sexo, situacao_vulnerabilidade,
               hipertenso, diabetico, gestacao
        FROM read_parquet('{DATA_DIR}/pacientes_anonimizados.parquet')
        WHERE endereco_latitude IS NOT NULL
          AND endereco_longitude IS NOT NULL
    """).df()
    in_bbox = (
        df["lon"].between(BBOX["lon_min"], BBOX["lon_max"]) &
        df["lat"].between(BBOX["lat_min"], BBOX["lat_max"])
    )
    return df[in_bbox].reset_index(drop=True)


def clusterizar(df: pd.DataFrame, k: int = 8) -> pd.DataFrame:
    n = len(df)
    k_eff = max(1, min(k, n // MIN_PACIENTES_POR_CLUSTER or 1))
    out = df.copy()
    if k_eff == 1:
        out["cluster_id"] = 0
        return out
    km = KMeans(n_clusters=k_eff, random_state=RANDOM_STATE, n_init=10)
    out["cluster_id"] = km.fit_predict(out[["lat", "lon"]].values)
    return out


def lista_inicial(df_cluster: pd.DataFrame, ubs_lat: float, ubs_lon: float) -> list[str]:
    """Ordem nearest-neighbor a partir da UBS — só pra primeira render não vir aleatória."""
    pendentes = df_cluster[["paciente_id", "lat", "lon"]].to_dict("records")
    ordem: list[str] = []
    cur_lat, cur_lon = ubs_lat, ubs_lon
    while pendentes:
        lats = np.array([p["lat"] for p in pendentes])
        lons = np.array([p["lon"] for p in pendentes])
        d2 = (lats - cur_lat) ** 2 + (lons - cur_lon) ** 2
        i = int(np.argmin(d2))
        ordem.append(pendentes[i]["paciente_id"])
        cur_lat, cur_lon = pendentes[i]["lat"], pendentes[i]["lon"]
        pendentes.pop(i)
    return ordem


# Conversão local degrees ↔ metros (planar; ok em raios pequenos no Rio ~22.9°S).
_M_PER_DEG_LAT = 111195.0
_M_PER_DEG_LON_RIO = 102392.0


def find_substitute_by_detour(
    df_panel: pd.DataFrame,
    route_coords: list[tuple[float, float]],
    exclude_pids: set[str],
    corridor_m: float = 300.0,
    max_detour_m: float = 400.0,
) -> tuple[str | None, int | None, float | None]:
    """Candidato off-list que minimiza detour ao ser inserido na rota planejada.

    `route_coords` em (lat, lon), na ordem (UBS + pendentes). Retorna
    `(paciente_id, insert_idx, detour_m)` onde `insert_idx = i` significa inserir
    entre `route_coords[i]` e `route_coords[i+1]`. Distâncias planares (Rio).

    Pré-filtro: candidato precisa estar a ≤`corridor_m` de algum waypoint atual.
    Limite duro: detour ≤ `max_detour_m`. Retorna `(None, None, None)` se ninguém qualifica.
    """
    cand = df_panel[~df_panel["paciente_id"].isin(exclude_pids)]
    if cand.empty or len(route_coords) < 2:
        return None, None, None

    ref_lat, ref_lon = route_coords[0]
    cx = (cand["lon"].to_numpy() - ref_lon) * _M_PER_DEG_LON_RIO
    cy = (cand["lat"].to_numpy() - ref_lat) * _M_PER_DEG_LAT
    pids = cand["paciente_id"].to_numpy()

    ry = np.array([(p[0] - ref_lat) * _M_PER_DEG_LAT for p in route_coords])
    rx = np.array([(p[1] - ref_lon) * _M_PER_DEG_LON_RIO for p in route_coords])
    seg_d = np.sqrt(np.diff(rx) ** 2 + np.diff(ry) ** 2)

    best_pid: str | None = None
    best_i: int | None = None
    best_detour = float("inf")
    for j in range(len(pids)):
        d_pts = np.sqrt((rx - cx[j]) ** 2 + (ry - cy[j]) ** 2)
        if d_pts.min() > corridor_m:
            continue
        detours = d_pts[:-1] + d_pts[1:] - seg_d
        i_local = int(np.argmin(detours))
        det = float(detours[i_local])
        if det <= max_detour_m and det < best_detour:
            best_detour = det
            best_pid = str(pids[j])
            best_i = i_local
    if best_pid is None:
        return None, None, None
    return best_pid, best_i, best_detour


def selecao_dia(
    df_cluster: pd.DataFrame,
    ubs_lat: float,
    ubs_lon: float,
    n: int,
    prev_status: dict[str, str] | None = None,
    followup_triggers: set[str] | None = None,
) -> list[str]:
    """
    Seleção dos N pacientes do dia.

    Placeholder enquanto ranker real não existe:
      1. Pacientes em status de follow-up no snapshot anterior entram primeiro
         (rerank — memory rerank-no-show). Default: "não atendeu" + "recusou atendimento".
      2. Resto preenchido por top-N nearest-neighbor a partir da UBS.
    """
    triggers = followup_triggers or {"não atendeu", "recusou atendimento"}
    pool_order = lista_inicial(df_cluster, ubs_lat, ubs_lon)
    if prev_status:
        followups = [p for p in pool_order if prev_status.get(p) in triggers]
    else:
        followups = []
    restantes = [p for p in pool_order if p not in set(followups)]
    return followups[:n] + restantes[: max(0, n - len(followups))]


def label_paciente(row: pd.Series) -> str:
    flags = []
    if row.get("hipertenso"):
        flags.append("HAS")
    if row.get("diabetico"):
        flags.append("DM")
    if row.get("gestacao"):
        flags.append("gestação")
    vuln = row.get("situacao_vulnerabilidade")
    if isinstance(vuln, str) and vuln and vuln.lower() not in ("nan", "none", ""):
        flags.append(vuln.lower())
    pid = str(row["paciente_id"])[:8]
    tail = f" · {' / '.join(flags)}" if flags else ""
    return f"{pid}{tail}"

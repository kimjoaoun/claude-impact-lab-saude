from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from lib.data import (
    clusterizar,
    find_substitute_by_detour,
    load_equipes,
    load_eventos_agg,
    load_pacientes,
    load_visitas_agg,
    selecao_dia,
)
from lib.osrm import OSRM_FOOT, OSRMError, osrm_health, osrm_route
from lib.state import latest_prior_snapshot, load_snapshot, save_snapshot, snapshots_in_range

STATUS_PENDENTE = "pendente"
STATUS_ATENDIDO = "atendeu"
STATUS_PARCIAL = "parcialmente atendeu"
STATUS_NO_SHOW = "não atendeu"
STATUS_RECUSOU = "recusou atendimento"
STATUS_CYCLE = [STATUS_PENDENTE, STATUS_ATENDIDO, STATUS_NO_SHOW, STATUS_RECUSOU]
STATUS_COLOR = {
    STATUS_PENDENTE: "#6c757d",
    STATUS_ATENDIDO: "#2ecc71",
    STATUS_PARCIAL: "#a3d977",
    STATUS_NO_SHOW: "#e74c3c",
    STATUS_RECUSOU: "#f39c12",
}
STATUS_REQUER_FOLLOWUP = {STATUS_NO_SHOW, STATUS_RECUSOU}
# Substituto entra perto do *percurso* da rota (não do paciente que faltou):
# pré-filtro de corredor + minimização de detour de inserção.
SUBSTITUTE_CORRIDOR_M = 300.0
SUBSTITUTE_MAX_DETOUR_M = 400.0


def next_status(s: str) -> str:
    try:
        idx = STATUS_CYCLE.index(s)
    except ValueError:
        return STATUS_PENDENTE
    return STATUS_CYCLE[(idx + 1) % len(STATUS_CYCLE)]


_STATUS_LEGADO = {"atendido": STATUS_ATENDIDO, "não atendido": STATUS_NO_SHOW}


def normalize_status(s: str | None) -> str:
    if s is None:
        return STATUS_PENDENTE
    s = _STATUS_LEGADO.get(s, s)
    return s if s in STATUS_CYCLE else STATUS_PENDENTE


def derive_family_status(member_status: dict[str, str]) -> str:
    normalized = [normalize_status(s) for s in member_status.values()]
    if not normalized:
        return STATUS_PENDENTE
    if all(s == STATUS_ATENDIDO for s in normalized):
        return STATUS_ATENDIDO
    if any(s == STATUS_ATENDIDO for s in normalized):
        return STATUS_PARCIAL
    if any(s == STATUS_RECUSOU for s in normalized):
        return STATUS_RECUSOU
    if any(s == STATUS_NO_SHOW for s in normalized):
        return STATUS_NO_SHOW
    return STATUS_PENDENTE


def build_member_statuses(
    family_lookup: dict[str, dict],
    family_order: list[str],
    family_status: dict[str, str] | None = None,
    existing_member_status: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for fid in family_order:
        members = family_lookup[fid]["members"]
        saved = (existing_member_status or {}).get(fid, {})
        base_status = normalize_status((family_status or {}).get(fid))
        out[fid] = {
            member["paciente_id"]: normalize_status(saved.get(member["paciente_id"], base_status))
            for member in members
        }
    return out


def compute_family_statuses(member_statuses: dict[str, dict[str, str]]) -> dict[str, str]:
    return {
        fid: derive_family_status(member_map)
        for fid, member_map in member_statuses.items()
    }

st.set_page_config(page_title="Rota ACS — dia", layout="wide")

st.markdown(
    """
    # 🚶 Rota do dia — ACS

    ACS sai da UBS com uma lista priorizada, **reordena livremente** no campo, e marca
    cada visita como atendido / não atendido. A rota no mapa segue a ordem do ACS
    (não reotimiza — `/route` do OSRM com waypoints fixos).

    > ⚠️ Coordenadas com ruído de anonimização (~100m + shuffle por equipe). Rotas e
    > durações são metodológicas, não operacionais. OSM mapeia mal vielas/escadarias
    > em comunidades.
    """
)

TODAY = date.today()
SELECTED_DATE_KEY = "selected_date"
FAMILY_PRIORITY_HIGH = "alta"
FAMILY_PRIORITY_MEDIUM = "média"
FAMILY_PRIORITY_LOW = "baixa"


def build_panel_key(paciente_ids: list[str]) -> str:
    raw = "|".join(sorted(str(pid) for pid in paciente_ids))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def validate_snapshot(
    snapshot: dict,
    *,
    panel_key: str,
    panel_pids: set[str],
    k_clusters: int,
    n_dia: int,
    panel_size: int,
) -> str | None:
    ordem = snapshot.get("ordem", [])
    if not isinstance(ordem, list):
        return "snapshot sem ordem válida"

    def _safe_int(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    saved_panel_key = snapshot.get("panel_key")
    if saved_panel_key is not None and saved_panel_key != panel_key:
        return "snapshot pertence a outro painel"

    saved_k_clusters = _safe_int(snapshot.get("k_clusters"))
    if saved_k_clusters is None and snapshot.get("k_clusters") is not None:
        return "snapshot tem k_clusters inválido"
    if saved_k_clusters is not None and saved_k_clusters != int(k_clusters):
        return "snapshot foi salvo com outro valor de k_clusters"

    saved_n_dia = _safe_int(snapshot.get("n_dia"))
    if saved_n_dia is None and snapshot.get("n_dia") is not None:
        return "snapshot tem n_dia inválido"
    if saved_n_dia is not None and saved_n_dia != int(n_dia):
        return "snapshot foi salvo com outro tamanho de lista do dia"

    saved_panel_size = _safe_int(snapshot.get("panel_size"))
    if saved_panel_size is None and snapshot.get("panel_size") is not None:
        return "snapshot tem panel_size inválido"
    if saved_panel_size is not None and saved_panel_size != int(panel_size):
        return "snapshot foi salvo para um painel com tamanho diferente"

    missing = [pid for pid in ordem if pid not in panel_pids]
    if missing:
        return f"snapshot contém {len(missing)} paciente(s) fora do painel atual"
    return None


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _safe_max_date(values: list[str | None]) -> str | None:
    valid = [v for v in values if v]
    return max(valid) if valid else None


def classify_family_priority(members: list[dict]) -> tuple[int, str]:
    score = 0
    if any(member["gestacao"] for member in members):
        score += 3
    if any(member["vulnerabilidade"] for member in members):
        score += 3
    if any(member["diabetico"] for member in members):
        score += 2
    if any(member["hipertenso"] for member in members):
        score += 1
    if sum(member["n_eventos"] for member in members) > 0:
        score += 1
    if score >= 4:
        return score, FAMILY_PRIORITY_HIGH
    if score >= 2:
        return score, FAMILY_PRIORITY_MEDIUM
    return score, FAMILY_PRIORITY_LOW


def is_priority_member(member: dict) -> bool:
    return bool(
        member["gestacao"]
        or member["vulnerabilidade"]
        or member["diabetico"]
        or member["n_eventos"] > 0
    )


def weekly_family_outcomes(
    week_snaps: list[dict],
    family_lookup: dict[str, dict],
) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for snap in week_snaps:
        snap_date = snap.get("dia")
        for fid, family_status in snap.get("status", {}).items():
            member_status = snap.get("member_status", {}).get(fid, {})
            priority_members = family_lookup.get(fid, {}).get("priority_member_ids", [])
            has_partial_priority_gap = bool(priority_members) and any(
                normalize_status(member_status.get(member_pid)) != STATUS_ATENDIDO
                for member_pid in priority_members
            )
            latest[fid] = {
                "date": snap_date,
                "status": normalize_status(family_status),
                "has_partial_priority_gap": has_partial_priority_gap,
            }
    return latest


def _member_detail(
    row,
    visitas_lookup: dict[str, dict],
    eventos_lookup: dict[str, dict],
) -> dict:
    pid = row["paciente_id"]
    v = visitas_lookup.get(pid, {})
    e = eventos_lookup.get(pid, {})
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
        "n_visitas": int(v.get("n_visitas", 0)),
        "ultima_visita": str(v["ultima_visita"]) if v.get("ultima_visita") is not None else None,
        "n_eventos": int(e.get("n_eventos", 0)),
        "ultimo_evento": str(e["ultimo_evento"]) if e.get("ultimo_evento") is not None else None,
        "tipos_eventos": str(e["tipos"]) if e.get("tipos") is not None else None,
    }


def build_family_panel(
    df_cluster,
    visitas_lookup: dict[str, dict],
    eventos_lookup: dict[str, dict],
):
    rows = []
    family_lookup: dict[str, dict] = {}
    for fid, grp in df_cluster.groupby("id_familia_sint", sort=False):
        members = [
            _member_detail(row, visitas_lookup, eventos_lookup)
            for _, row in grp.iterrows()
        ]
        priority_score, priority_band = classify_family_priority(members)
        priority_member_ids = [
            member["paciente_id"]
            for member in members
            if is_priority_member(member)
        ]
        if not priority_member_ids and members:
            priority_member_ids = [members[0]["paciente_id"]]
        flags = []
        if any(m["gestacao"] for m in members):
            flags.append("gestação")
        if any(m["diabetico"] for m in members):
            flags.append("DM")
        if any(m["hipertenso"] for m in members):
            flags.append("HAS")
        if any(m["vulnerabilidade"] for m in members):
            flags.append("vulnerab.")
        flags_str = " · " + " / ".join(flags[:3]) if flags else ""
        label = f"{len(members)} morador(es){flags_str}"
        family_summary = {
            "id_familia_sint": fid,
            "lat": float(grp["lat"].mean()),
            "lon": float(grp["lon"].mean()),
            "member_count": int(len(members)),
            "label": label,
            "members": members,
            "priority_score": int(priority_score),
            "priority_band": priority_band,
            "priority_member_ids": priority_member_ids,
            "n_visitas_total": int(sum(m["n_visitas"] for m in members)),
            "ultima_visita": _safe_max_date([m["ultima_visita"] for m in members]),
            "n_eventos_total": int(sum(m["n_eventos"] for m in members)),
            "ultimo_evento": _safe_max_date([m["ultimo_evento"] for m in members]),
        }
        family_lookup[fid] = family_summary
        rows.append({
            "paciente_id": fid,
            "lat": family_summary["lat"],
            "lon": family_summary["lon"],
            "priority_score": family_summary["priority_score"],
            "priority_band": family_summary["priority_band"],
        })
    return pd.DataFrame(rows), family_lookup


def family_popup_html(family: dict) -> str:
    member_lines = "".join(
        (
            f"<li><b>{m['paciente_id'][:10]}…</b> · "
            f"{m.get('sexo') or '—'} · {m.get('faixa_etaria') or '—'}</li>"
        )
        for m in family["members"][:6]
    )
    return f"""
    <div style="font-family:system-ui;font-size:12px;min-width:260px">
      <b>{family['id_familia_sint']}</b><br/>
      <b>Domicílio:</b> {family['member_count']} morador(es)<br/>
      <b>Visitas:</b> {family['n_visitas_total']} (última: {family['ultima_visita'] or '—'})<br/>
      <b>Eventos:</b> {family['n_eventos_total']} (último: {family['ultimo_evento'] or '—'})<br/>
      <b>Membros:</b>
      <ul style="margin:6px 0 0 16px;padding:0">{member_lines}</ul>
    </div>
    """

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Configuração")
    if not osrm_health():
        st.error(
            f"OSRM foot indisponível em `{OSRM_FOOT}`.\n\n"
            "Rode: `cd routing && docker compose --profile routing up -d osrm-foot`"
        )
        st.stop()
    st.success(f"OSRM foot ✓ ({OSRM_FOOT})")

    equipes = load_equipes()
    pacientes_all = load_pacientes()

    k_clusters = st.number_input("K clusters por equipe (= ACS)", min_value=1, max_value=20, value=8)
    n_dia = st.slider("N domicílios na lista do dia", min_value=5, max_value=25, value=12)
    st.caption(
        "Seleção atual: top-N nearest-neighbor + boost de no-shows do dia anterior. "
        "TODO: ranker real (risco + care-gap)."
    )

    equipes_validas = sorted(
        set(equipes["equipe_id"]) & set(pacientes_all["equipe_id"].unique())
    )
    equipe_id = st.selectbox(
        "Equipe",
        options=equipes_validas,
        format_func=lambda eid: f"{eid[:8]}… ({(pacientes_all['equipe_id'] == eid).sum()} pacientes)",
    )

    pacientes_eq = pacientes_all[pacientes_all["equipe_id"] == equipe_id].reset_index(drop=True)
    pacientes_eq = clusterizar(pacientes_eq, k=int(k_clusters))

    cluster_id = st.selectbox(
        "Cluster (panel hipotético do ACS)",
        options=sorted(pacientes_eq["cluster_id"].unique()),
        format_func=lambda c: f"cluster {c} ({(pacientes_eq['cluster_id'] == c).sum()} pac.)",
    )

    st.divider()
    if SELECTED_DATE_KEY not in st.session_state:
        st.session_state[SELECTED_DATE_KEY] = TODAY

    nav1, nav2 = st.columns(2)
    if nav1.button("⬅︎ -1 dia"):
        st.session_state[SELECTED_DATE_KEY] = st.session_state[SELECTED_DATE_KEY] - timedelta(days=1)
        st.rerun()
    if nav2.button("+1 dia ➡︎"):
        st.session_state[SELECTED_DATE_KEY] = st.session_state[SELECTED_DATE_KEY] + timedelta(days=1)
        st.rerun()

    selected_date = st.date_input("📅 Dia", key=SELECTED_DATE_KEY)
    if selected_date < TODAY:
        st.caption("🕒 Dia passado — read-only")
    elif selected_date > TODAY:
        st.caption("📅 Planejamento futuro — editável")
    else:
        st.caption("📍 Hoje — editável")

    if st.button("🔄 Resetar dia (in-memory)"):
        for k in list(st.session_state.keys()):
            if k.startswith("dia_"):
                del st.session_state[k]
        st.rerun()


# ─── Resolve estado do dia selecionado ───────────────────────────────────────
ubs = equipes.set_index("equipe_id").loc[equipe_id]
sub = pacientes_eq[pacientes_eq["cluster_id"] == cluster_id].reset_index(drop=True)
visitas_agg = load_visitas_agg()
eventos_agg = load_eventos_agg()
visitas_lookup = visitas_agg.set_index("paciente_id").to_dict("index")
eventos_lookup = eventos_agg.set_index("paciente_id").to_dict("index")
family_rows, family_lookup = build_family_panel(sub, visitas_lookup, eventos_lookup)
familias_panel = family_rows
panel_pids = set(familias_panel["paciente_id"].tolist())
panel_key = build_panel_key(sorted(panel_pids))
coords_lookup = {
    row["paciente_id"]: {"lat": row["lat"], "lon": row["lon"]}
    for _, row in familias_panel.iterrows()
}
label_lookup = {
    fid: (
        f"{fid[:16]}… · {family_lookup[fid]['priority_band']} · {family_lookup[fid]['label']}"
    )
    for fid in panel_pids
}

is_past = selected_date < TODAY
existing_snapshot = load_snapshot(equipe_id, int(cluster_id), panel_key, selected_date)
snapshot_issue = None
if existing_snapshot is not None:
    snapshot_issue = validate_snapshot(
        existing_snapshot,
        panel_key=panel_key,
        panel_pids=panel_pids,
        k_clusters=int(k_clusters),
        n_dia=int(n_dia),
        panel_size=int(len(familias_panel)),
    )
    if snapshot_issue:
        existing_snapshot = None

if is_past:
    if snapshot_issue:
        st.error(
            f"Snapshot incompatível para {selected_date.isoformat()} "
            f"(equipe {equipe_id[:8]}… / cluster {cluster_id}): {snapshot_issue}."
        )
        st.stop()
    if existing_snapshot is None:
        st.warning(
            f"Sem registro salvo pra {selected_date.isoformat()} "
            f"(equipe {equipe_id[:8]}… / cluster {cluster_id}). "
            "Dias passados só são visualizados a partir de snapshots exportados."
        )
        st.stop()
    ordem = list(existing_snapshot["ordem"])
    status_legacy = {pid: normalize_status(existing_snapshot["status"].get(pid)) for pid in ordem}
    member_status = build_member_statuses(
        family_lookup,
        ordem,
        family_status=status_legacy,
        existing_member_status=existing_snapshot.get("member_status"),
    )
    status = compute_family_statuses(member_status)
    substitutes = existing_snapshot.get("substitutes", {})
    read_only = True
else:
    if snapshot_issue:
        st.warning(
            f"Snapshot salvo ignorado para {selected_date.isoformat()}: {snapshot_issue}. "
            "A lista do dia foi regenerada para o painel atual."
        )
    # Hoje ou futuro: ou retoma snapshot já salvo, ou gera com rerank.
    if existing_snapshot is not None:
        ordem_inicial = list(existing_snapshot["ordem"])
        status_inicial_legacy = {
            pid: normalize_status(existing_snapshot["status"].get(pid))
            for pid in ordem_inicial
        }
        member_status_inicial = build_member_statuses(
            family_lookup,
            ordem_inicial,
            family_status=status_inicial_legacy,
            existing_member_status=existing_snapshot.get("member_status"),
        )
        status_inicial = compute_family_statuses(member_status_inicial)
    else:
        prev = latest_prior_snapshot(equipe_id, int(cluster_id), panel_key, selected_date)
        prev_status = (
            {pid: normalize_status(s) for pid, s in prev["status"].items()} if prev else None
        )
        cooldown_pids: set[str] = set()
        week_snaps = snapshots_in_range(
            equipe_id,
            int(cluster_id),
            panel_key,
            week_start(selected_date),
            selected_date - timedelta(days=1),
        )
        weekly_outcomes = weekly_family_outcomes(week_snaps, family_lookup)
        for snap in week_snaps:
            for pid, snap_status in snap.get("status", {}).items():
                if normalize_status(snap_status) == STATUS_ATENDIDO:
                    cooldown_pids.add(pid)
        forced_front_pids = {
            fid
            for fid, outcome in weekly_outcomes.items()
            if family_lookup.get(fid, {}).get("priority_band") == FAMILY_PRIORITY_HIGH
            and (
                outcome["status"] == STATUS_NO_SHOW
                or outcome.get("has_partial_priority_gap")
            )
            and outcome.get("date")
            and date.fromisoformat(outcome["date"]) <= (selected_date - timedelta(days=1))
        }
        ordem_inicial = selecao_dia(
            familias_panel,
            ubs["ubs_lat"],
            ubs["ubs_lon"],
            int(n_dia),
            prev_status,
            cooldown_pids=cooldown_pids,
            forced_front_pids=forced_front_pids,
        )
        member_status_inicial = build_member_statuses(family_lookup, ordem_inicial)
        status_inicial = compute_family_statuses(member_status_inicial)

    key_ordem = f"dia_ordem_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"
    key_member_status = (
        f"dia_member_status_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"
    )
    key_subst = f"dia_subst_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"

    if key_ordem not in st.session_state:
        st.session_state[key_ordem] = ordem_inicial
        st.session_state[key_member_status] = member_status_inicial
        st.session_state[key_subst] = (existing_snapshot or {}).get("substitutes", {})

    ordem = st.session_state[key_ordem]
    member_status = st.session_state[key_member_status]
    status = compute_family_statuses(member_status)
    substitutes = st.session_state[key_subst]  # trigger_pid -> substitute_pid | None

    # ─── Auto-substituição: candidato que minimiza detour da rota restante ────
    auto_substitute_toasts: list[tuple[str, str | None]] = []
    auto_substitute_changed = False
    for trigger_pid in list(ordem):
        if status.get(trigger_pid) not in STATUS_REQUER_FOLLOWUP:
            continue
        if trigger_pid in substitutes:
            continue
        route_pids_now = [p for p in ordem if status.get(p) not in STATUS_REQUER_FOLLOWUP]
        route_coords_now = [(ubs["ubs_lat"], ubs["ubs_lon"])] + [
            (coords_lookup[p]["lat"], coords_lookup[p]["lon"]) for p in route_pids_now
        ]
        sub_pid, insert_i, detour_m = find_substitute_by_detour(
            familias_panel, route_coords_now,
            exclude_pids=set(ordem),
            corridor_m=SUBSTITUTE_CORRIDOR_M,
            max_detour_m=SUBSTITUTE_MAX_DETOUR_M,
        )
        substitutes[trigger_pid] = sub_pid
        if sub_pid:
            # insert_i é índice em route_coords_now: 0 = antes do primeiro pendente,
            # i>=1 = depois de route_pids_now[i-1]. Traduzir pra posição em `ordem`.
            new_ordem = list(ordem)
            if insert_i == 0 and route_pids_now:
                anchor = route_pids_now[0]
                new_ordem.insert(new_ordem.index(anchor), sub_pid)
            elif insert_i is not None and insert_i >= 1:
                anchor = route_pids_now[insert_i - 1]
                new_ordem.insert(new_ordem.index(anchor) + 1, sub_pid)
            else:
                new_ordem.append(sub_pid)
            ordem = new_ordem
            st.session_state[key_ordem] = ordem
            member_status[sub_pid] = {
                member["paciente_id"]: STATUS_PENDENTE
                for member in family_lookup[sub_pid]["members"]
            }
            auto_substitute_changed = True
            auto_substitute_toasts.append((
                f"🔁 substituto (detour +{int(detour_m)}m): "
                f"{label_lookup.get(sub_pid, sub_pid[:8])}",
                None,
            ))
        else:
            auto_substitute_changed = True
            auto_substitute_toasts.append((
                f"⚠️ sem substituto viável (corredor {int(SUBSTITUTE_CORRIDOR_M)}m / "
                f"detour ≤{int(SUBSTITUTE_MAX_DETOUR_M)}m) pra "
                f"{label_lookup.get(trigger_pid, trigger_pid[:8])}",
                "⚠️",
            ))

    if auto_substitute_changed:
        st.session_state[key_member_status] = member_status
        st.session_state[key_subst] = substitutes
        for message, icon in auto_substitute_toasts:
            st.toast(message, icon=icon)

    read_only = False


# ─── Layout ───────────────────────────────────────────────────────────────────
col_lista, col_mapa = st.columns([1, 2])

with col_lista:
    header = f"Lista — {selected_date.isoformat()}"
    if read_only:
        header += " · 🔒 read-only"
    st.subheader(header)

    # Mostra contagem de no-shows herdados (só pra hoje/futuro)
    if not read_only:
        prev = latest_prior_snapshot(equipe_id, int(cluster_id), panel_key, selected_date)
        if prev:
            n_followup_prev = sum(
                1 for s in prev["status"].values()
                if normalize_status(s) in STATUS_REQUER_FOLLOWUP
            )
            if n_followup_prev:
                st.info(
                    f"♻️ {n_followup_prev} follow-up(s) do dia {prev['exported_at'][:10]} "
                    "puxado(s) pro topo da lista."
                )
        week_snaps = snapshots_in_range(
            equipe_id,
            int(cluster_id),
            panel_key,
            week_start(selected_date),
            selected_date - timedelta(days=1),
        )
        weekly_outcomes = weekly_family_outcomes(week_snaps, family_lookup)
        cooldown_pids = {
            pid
            for snap in week_snaps
            for pid, snap_status in snap.get("status", {}).items()
            if normalize_status(snap_status) == STATUS_ATENDIDO
        }
        if cooldown_pids:
            st.caption(
                f"🗓️ {len(cooldown_pids)} domicílio(s) já atendido(s) nesta semana "
                "foram empurrado(s) para o fim da prioridade."
            )
        forced_same_week = {
            fid
            for fid, outcome in weekly_outcomes.items()
            if family_lookup.get(fid, {}).get("priority_band") == FAMILY_PRIORITY_HIGH
            and (
                outcome["status"] == STATUS_NO_SHOW
                or outcome.get("has_partial_priority_gap")
            )
            and outcome.get("date")
            and date.fromisoformat(outcome["date"]) <= (selected_date - timedelta(days=1))
        }
        if forced_same_week:
            st.caption(
                f"🚨 {len(forced_same_week)} domicílio(s) de alta prioridade com "
                "morador prioritário pendente foram reagendado(s) para esta semana."
            )

    last_idx = len(ordem) - 1
    subst_pids = {v for v in substitutes.values() if v}
    for i, pid in enumerate(ordem):
        cur_status = status.get(pid, STATUS_PENDENTE)
        color = STATUS_COLOR[cur_status]
        is_subst = pid in subst_pids
        with st.container(border=True):
            top = st.columns([1, 8, 2])
            with top[0]:
                st.markdown(f"**{i+1:02d}.**")
            with top[1]:
                prefix = "🔁 " if is_subst else ""
                st.markdown(f"**{prefix}{label_lookup[pid]}**")
            with top[2]:
                st.markdown(
                    f"<div style='text-align:right'><span style='background:{color};"
                    f"color:white;padding:2px 8px;border-radius:6px;font-size:11px'>"
                    f"{cur_status}</span></div>",
                    unsafe_allow_html=True,
                )

            if not read_only:
                row = st.columns([1, 1, 6])
                up_disabled = i == 0
                dn_disabled = i == last_idx
                key_suffix = f"{equipe_id}_{cluster_id}_{selected_date.isoformat()}_{pid}"
                if row[0].button("⬆︎", key=f"up_{key_suffix}", disabled=up_disabled,
                                 use_container_width=True):
                    new_ordem = list(ordem)
                    new_ordem[i - 1], new_ordem[i] = new_ordem[i], new_ordem[i - 1]
                    st.session_state[key_ordem] = new_ordem
                    st.rerun()
                if row[1].button("⬇︎", key=f"dn_{key_suffix}", disabled=dn_disabled,
                                 use_container_width=True):
                    new_ordem = list(ordem)
                    new_ordem[i + 1], new_ordem[i] = new_ordem[i], new_ordem[i + 1]
                    st.session_state[key_ordem] = new_ordem
                    st.rerun()
                with row[2]:
                    st.markdown(
                        f"<div style='text-align:center'><span style='background:{STATUS_COLOR[cur_status]};"
                        f"color:white;padding:6px 10px;border-radius:6px;font-size:12px;display:inline-block'>"
                        f"domicílio: {cur_status}</span></div>",
                        unsafe_allow_html=True,
                    )

            with st.expander("ⓘ detalhes"):
                family = family_lookup[pid]
                st.markdown(
                    f"**Domicílio:** `{family['id_familia_sint']}`  \n"
                    f"**Moradores:** {family['member_count']}  \n"
                    f"**Prioridade familiar:** {family['priority_band']}  \n"
                    f"**Moradores prioritários:** {len(family['priority_member_ids'])}  \n"
                    f"**Visitas históricas:** {family['n_visitas_total']}"
                    + (f" · última {family['ultima_visita']}" if family['ultima_visita'] else "")
                    + f"  \n**Eventos clínicos:** {family['n_eventos_total']}"
                    + (f" · último {family['ultimo_evento']}" if family['ultimo_evento'] else "")
                )
                st.markdown("**Membros**")
                for member in family["members"]:
                    member_pid = member["paciente_id"]
                    member_cur_status = member_status[pid][member_pid]
                    is_priority = member_pid in family["priority_member_ids"]
                    flags = []
                    if member["hipertenso"]:
                        flags.append("HAS")
                    if member["diabetico"]:
                        flags.append("DM")
                    if member["gestacao"]:
                        flags.append("gestação")
                    member_cols = st.columns([6, 3])
                    with member_cols[0]:
                        prefix = "**prioritário** · " if is_priority else ""
                        st.markdown(
                            prefix
                            + f"`{member_pid[:12]}…` · "
                            f"{member.get('sexo') or '—'} · {member.get('faixa_etaria') or '—'}"
                            + (f" · {' / '.join(flags)}" if flags else "")
                            + (
                                f" · vuln: {member.get('vulnerabilidade')}"
                                if member.get('vulnerabilidade') else ""
                            )
                            + (
                                f"  \nHistórico: {member['n_visitas']} visita(s)"
                                + (
                                    f" · última {member['ultima_visita']}"
                                    if member['ultima_visita'] else ""
                                )
                            )
                            + (
                                f"  \nEventos: {member['n_eventos']}"
                                + (
                                    f" · último {member['ultimo_evento']}"
                                    if member['ultimo_evento'] else ""
                                )
                                if member["n_eventos"] else ""
                            )
                        )
                    with member_cols[1]:
                        if read_only:
                            st.markdown(
                                f"<div style='text-align:right'><span style='background:{STATUS_COLOR[member_cur_status]};"
                                f"color:white;padding:2px 8px;border-radius:6px;font-size:11px'>"
                                f"{member_cur_status}</span></div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            if st.button(
                                f"{member_cur_status} ↻",
                                key=f"member_{key_suffix}_{member_pid}",
                                use_container_width=True,
                            ):
                                member_status[pid][member_pid] = next_status(member_cur_status)
                                st.session_state[key_member_status] = member_status
                                st.rerun()

# ─── Rota (respeita ordem, pula quem precisa de follow-up) ────────────────────
waypoints_pids = [pid for pid in ordem if status.get(pid) not in STATUS_REQUER_FOLLOWUP]
coords = [(ubs["ubs_lon"], ubs["ubs_lat"])] + [
    (coords_lookup[pid]["lon"], coords_lookup[pid]["lat"]) for pid in waypoints_pids
]

route_geom = None
route_dur_s = 0.0
route_dist_m = 0.0
route_err = None
if len(coords) >= 2:
    try:
        rt = osrm_route(coords)
        route_geom = rt["geometry"]
        route_dur_s = rt["duration"]
        route_dist_m = rt["distance"]
    except (OSRMError, Exception) as e:
        route_err = str(e)

# ─── Métricas + export ────────────────────────────────────────────────────────
with col_lista:
    st.divider()
    n_pend = sum(1 for s in status.values() if s == STATUS_PENDENTE)
    n_aten = sum(1 for s in status.values() if s == STATUS_ATENDIDO)
    n_parcial = sum(1 for s in status.values() if s == STATUS_PARCIAL)
    n_no = sum(1 for s in status.values() if s == STATUS_NO_SHOW)
    n_rec = sum(1 for s in status.values() if s == STATUS_RECUSOU)
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Pendente", n_pend)
    m2.metric("Atendeu", n_aten)
    m3.metric("Parcial", n_parcial)
    m4.metric("Não atend.", n_no)
    m5.metric("Recusou", n_rec)
    m6.metric("Rota", f"{route_dur_s/3600:.1f}h" if route_dur_s else "—")

    if route_err:
        st.warning(f"OSRM: {route_err}")

    if not read_only and ordem:
        status = compute_family_statuses(member_status)
        payload = {
            "equipe_id": equipe_id,
            "cluster_id": int(cluster_id),
            "k_clusters": int(k_clusters),
            "dia": selected_date.isoformat(),
            "n_dia": int(n_dia),
            "panel_key": panel_key,
            "panel_size": int(len(familias_panel)),
            "selecao_dia_strategy": (
                "top-N domicílios por centróide + rerank no-show + cooldown semanal de atendidos"
            ),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "ordem": ordem,
            "status": status,
            "member_status": member_status,
            "substitutes": substitutes,
            "rota": {"duration_s": route_dur_s, "distance_m": route_dist_m},
        }
        out = save_snapshot(payload, equipe_id, int(cluster_id), panel_key, selected_date)
        st.caption(f"💾 autosave · {datetime.now().strftime('%H:%M:%S')} · `{out.name}`")

# ─── Mapa ─────────────────────────────────────────────────────────────────────
with col_mapa:
    st.subheader("Mapa")
    m = folium.Map(
        location=[ubs["ubs_lat"], ubs["ubs_lon"]],
        zoom_start=14,
        tiles="CartoDB positron",
    )
    folium.Marker(
        location=[ubs["ubs_lat"], ubs["ubs_lon"]],
        icon=folium.Icon(color="black", icon="plus-sign"),
        popup="UBS",
    ).add_to(m)

    for i, pid in enumerate(ordem, start=1):
        c = coords_lookup[pid]
        color = STATUS_COLOR[status.get(pid, STATUS_PENDENTE)]
        family = family_lookup[pid]
        folium.CircleMarker(
            location=[c["lat"], c["lon"]],
            radius=10,
            color=color,
            fill=True,
            fill_opacity=0.85,
            weight=2,
            tooltip=f"{i:02d}. {label_lookup[pid]} — {status.get(pid)} (clique pra detalhes)",
            popup=folium.Popup(family_popup_html(family), max_width=320),
        ).add_to(m)
        folium.map.Marker(
            [c["lat"], c["lon"]],
            icon=folium.DivIcon(
                icon_size=(20, 20),
                icon_anchor=(10, 10),
                html=f'<div style="font-size:10px;color:white;text-align:center;font-weight:bold">{i}</div>',
            ),
        ).add_to(m)

    if route_geom is not None:
        latlon = [(lat, lon) for lon, lat in route_geom["coordinates"]]
        folium.PolyLine(
            locations=latlon, color="#4363d8", weight=4, opacity=0.7,
            tooltip=f"{route_dur_s/3600:.1f}h · {route_dist_m/1000:.1f}km",
        ).add_to(m)

    st_folium(m, height=600, use_container_width=True, returned_objects=[])

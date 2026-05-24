from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

from lib.data import (
    clusterizar,
    find_substitute_by_detour,
    label_paciente,
    load_equipes,
    load_eventos_agg,
    load_pacientes,
    load_visitas_agg,
    paciente_detalhes,
    selecao_dia,
)
from lib.osrm import OSRM_FOOT, OSRMError, osrm_health, osrm_route
from lib.state import latest_prior_snapshot, load_snapshot, save_snapshot

STATUS_PENDENTE = "pendente"
STATUS_ATENDIDO = "atendeu"
STATUS_NO_SHOW = "não atendeu"
STATUS_RECUSOU = "recusou atendimento"
STATUS_CYCLE = [STATUS_PENDENTE, STATUS_ATENDIDO, STATUS_NO_SHOW, STATUS_RECUSOU]
STATUS_COLOR = {
    STATUS_PENDENTE: "#6c757d",
    STATUS_ATENDIDO: "#2ecc71",
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
    n_dia = st.slider("N pacientes na lista do dia", min_value=5, max_value=25, value=12)
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
        format_func=lambda eid: f"{eid[:8]}… ({(pacientes_all['equipe_id'] == eid).sum()} pac.)",
    )

    pacientes_eq = pacientes_all[pacientes_all["equipe_id"] == equipe_id].reset_index(drop=True)
    pacientes_eq = clusterizar(pacientes_eq, k=int(k_clusters))

    cluster_id = st.selectbox(
        "Cluster (panel hipotético do ACS)",
        options=sorted(pacientes_eq["cluster_id"].unique()),
        format_func=lambda c: f"cluster {c} ({(pacientes_eq['cluster_id'] == c).sum()} pac.)",
    )

    st.divider()
    selected_date = st.date_input("📅 Dia", value=TODAY)
    if selected_date < TODAY:
        st.caption("🕒 Dia passado — read-only")
    elif selected_date > TODAY:
        st.caption("📅 Planejamento futuro — editável")
    else:
        st.caption("📍 Hoje — editável")

    nav1, nav2 = st.columns(2)
    if nav1.button("⬅︎ -1 dia"):
        st.session_state["sel_date"] = (selected_date.toordinal() - 1)
        st.rerun()
    if nav2.button("+1 dia ➡︎"):
        st.session_state["sel_date"] = (selected_date.toordinal() + 1)
        st.rerun()
    if "sel_date" in st.session_state:
        selected_date = date.fromordinal(st.session_state.pop("sel_date"))

    if st.button("🔄 Resetar dia (in-memory)"):
        for k in list(st.session_state.keys()):
            if k.startswith("dia_"):
                del st.session_state[k]
        st.rerun()


# ─── Resolve estado do dia selecionado ───────────────────────────────────────
ubs = equipes.set_index("equipe_id").loc[equipe_id]
sub = pacientes_eq[pacientes_eq["cluster_id"] == cluster_id].reset_index(drop=True)
coords_lookup = sub.set_index("paciente_id")[["lat", "lon"]].to_dict("index")
label_lookup = {row["paciente_id"]: label_paciente(row) for _, row in sub.iterrows()}

visitas_agg = load_visitas_agg()
eventos_agg = load_eventos_agg()
row_by_pid = {row["paciente_id"]: row for _, row in sub.iterrows()}


def _detalhes(pid: str) -> dict:
    return paciente_detalhes(row_by_pid[pid], visitas_agg, eventos_agg)


def _detalhes_html(d: dict) -> str:
    flags = []
    if d["hipertenso"]:
        flags.append("HAS")
    if d["diabetico"]:
        flags.append("DM")
    if d["gestacao"]:
        flags.append("gestação")
    flags_str = " · ".join(flags) if flags else "—"
    return f"""
    <div style="font-family:system-ui;font-size:12px;min-width:240px">
      <b>{d['paciente_id'][:12]}…</b><br/>
      <span style="color:#555">{d.get('sexo') or '—'} · {d.get('faixa_etaria') or '—'}</span><br/>
      <b>Condições:</b> {flags_str}<br/>
      <b>Vulnerab.:</b> {d.get('vulnerabilidade') or '—'}<br/>
      <b>Visitas:</b> {d['n_visitas']} (última: {d['ultima_visita'] or '—'})<br/>
      <b>Eventos:</b> {d['n_eventos']} (último: {d['ultimo_evento'] or '—'})<br/>
      <span style="color:#777;font-size:10px">{d.get('tipos_eventos') or ''}</span>
    </div>
    """

is_past = selected_date < TODAY
is_future = selected_date > TODAY
existing_snapshot = load_snapshot(equipe_id, int(cluster_id), selected_date)

if is_past:
    if existing_snapshot is None:
        st.warning(
            f"Sem registro salvo pra {selected_date.isoformat()} "
            f"(equipe {equipe_id[:8]}… / cluster {cluster_id}). "
            "Dias passados só são visualizados a partir de snapshots exportados."
        )
        st.stop()
    ordem = [pid for pid in existing_snapshot["ordem"] if pid in coords_lookup]
    status = {pid: normalize_status(existing_snapshot["status"].get(pid)) for pid in ordem}
    substitutes = existing_snapshot.get("substitutes", {})
    read_only = True
else:
    # Hoje ou futuro: ou retoma snapshot já salvo, ou gera com rerank.
    if existing_snapshot is not None:
        ordem_inicial = [pid for pid in existing_snapshot["ordem"] if pid in coords_lookup]
        status_inicial = {
            pid: normalize_status(existing_snapshot["status"].get(pid))
            for pid in ordem_inicial
        }
    else:
        prev = latest_prior_snapshot(equipe_id, int(cluster_id), selected_date)
        prev_status = (
            {pid: normalize_status(s) for pid, s in prev["status"].items()} if prev else None
        )
        ordem_inicial = selecao_dia(
            sub, ubs["ubs_lat"], ubs["ubs_lon"], int(n_dia), prev_status
        )
        status_inicial = {pid: STATUS_PENDENTE for pid in ordem_inicial}

    key_ordem = f"dia_ordem_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"
    key_status = f"dia_status_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"
    key_subst = f"dia_subst_{equipe_id}_{k_clusters}_{cluster_id}_{n_dia}_{selected_date.isoformat()}"

    if key_ordem not in st.session_state:
        st.session_state[key_ordem] = ordem_inicial
        st.session_state[key_status] = status_inicial
        st.session_state[key_subst] = (existing_snapshot or {}).get("substitutes", {})

    ordem = st.session_state[key_ordem]
    status = st.session_state[key_status]
    substitutes = st.session_state[key_subst]  # trigger_pid -> substitute_pid | None

    # ─── Auto-substituição: candidato que minimiza detour da rota restante ────
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
            sub, route_coords_now,
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
            st.session_state[key_ordem] = new_ordem
            status[sub_pid] = STATUS_PENDENTE
            st.toast(
                f"🔁 substituto (detour +{int(detour_m)}m): "
                f"{label_lookup.get(sub_pid, sub_pid[:8])}"
            )
            st.rerun()
        else:
            st.toast(
                f"⚠️ sem substituto viável (corredor {int(SUBSTITUTE_CORRIDOR_M)}m / "
                f"detour ≤{int(SUBSTITUTE_MAX_DETOUR_M)}m) pra "
                f"{label_lookup.get(trigger_pid, trigger_pid[:8])}",
                icon="⚠️",
            )

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
        prev = latest_prior_snapshot(equipe_id, int(cluster_id), selected_date)
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
                    if st.button(
                        f"{cur_status}  ↻",
                        key=f"cyc_{key_suffix}",
                        use_container_width=True,
                    ):
                        status[pid] = next_status(cur_status)
                        st.rerun()

            with st.expander("ⓘ detalhes"):
                d = _detalhes(pid)
                flags = []
                if d["hipertenso"]:
                    flags.append("HAS")
                if d["diabetico"]:
                    flags.append("DM")
                if d["gestacao"]:
                    flags.append("gestação")
                st.markdown(
                    f"**ID:** `{d['paciente_id'][:16]}…`  \n"
                    f"**Demografia:** {d.get('sexo') or '—'} · {d.get('faixa_etaria') or '—'}  \n"
                    f"**Condições:** {' · '.join(flags) if flags else '—'}  \n"
                    f"**Vulnerabilidade:** {d.get('vulnerabilidade') or '—'}  \n"
                    f"**Visitas históricas:** {d['n_visitas']}"
                    + (f" · última {d['ultima_visita']}" if d['ultima_visita'] else "")
                    + f"  \n**Eventos clínicos:** {d['n_eventos']}"
                    + (f" · último {d['ultimo_evento']}" if d['ultimo_evento'] else "")
                    + (f"  \n*Tipos:* {d['tipos_eventos']}" if d['tipos_eventos'] else "")
                )

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
    n_no = sum(1 for s in status.values() if s == STATUS_NO_SHOW)
    n_rec = sum(1 for s in status.values() if s == STATUS_RECUSOU)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Pendente", n_pend)
    m2.metric("Atendeu", n_aten)
    m3.metric("Não atend.", n_no)
    m4.metric("Recusou", n_rec)
    m5.metric("Rota", f"{route_dur_s/3600:.1f}h" if route_dur_s else "—")

    if route_err:
        st.warning(f"OSRM: {route_err}")

    if not read_only and ordem:
        payload = {
            "equipe_id": equipe_id,
            "cluster_id": int(cluster_id),
            "dia": selected_date.isoformat(),
            "n_dia": int(n_dia),
            "panel_size": int(len(sub)),
            "selecao_dia_strategy": "top-N nearest-neighbor + rerank no-show (placeholder)",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "ordem": ordem,
            "status": status,
            "substitutes": substitutes,
            "rota": {"duration_s": route_dur_s, "distance_m": route_dist_m},
        }
        out = save_snapshot(payload, equipe_id, int(cluster_id), selected_date)
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
        d = _detalhes(pid)
        folium.CircleMarker(
            location=[c["lat"], c["lon"]],
            radius=10,
            color=color,
            fill=True,
            fill_opacity=0.85,
            weight=2,
            tooltip=f"{i:02d}. {label_lookup[pid]} — {status.get(pid)} (clique pra detalhes)",
            popup=folium.Popup(_detalhes_html(d), max_width=320),
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

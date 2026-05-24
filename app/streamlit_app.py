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

st.set_page_config(page_title="Radar Família", layout="wide")

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


def month_start(d: date) -> date:
    return d.replace(day=1)


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


def family_outcomes_in_range(
    snaps: list[dict],
    family_lookup: dict[str, dict],
) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for snap in snaps:
        snap_date = snap.get("dia")
        for fid, family_status in snap.get("status", {}).items():
            member_status = snap.get("member_status", {}).get(fid, {})
            priority_members = family_lookup.get(fid, {}).get("priority_member_ids", [])
            has_partial_priority_gap = bool(priority_members) and any(
                normalize_status(member_status.get(member_pid)) != STATUS_ATENDIDO
                for member_pid in priority_members
            )
            derived = derive_family_status(member_status) if member_status else normalize_status(family_status)
            latest[fid] = {
                "date": snap_date,
                "status": derived,
                "has_partial_priority_gap": has_partial_priority_gap,
            }
    return latest


def month_fulfilled_pids(monthly_outcomes: dict[str, dict]) -> set[str]:
    fulfilled: set[str] = set()
    for fid, outcome in monthly_outcomes.items():
        if outcome["status"] == STATUS_ATENDIDO:
            fulfilled.add(fid)
        elif outcome["status"] == STATUS_PARCIAL and not outcome.get("has_partial_priority_gap"):
            fulfilled.add(fid)
    return fulfilled


# alias para snapshots de chamada antiga
weekly_family_outcomes = family_outcomes_in_range


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


def build_print_html(
    *,
    equipe_id: str,
    cluster_id: int,
    acs_label: str,
    selected_date: date,
    ubs_lat: float,
    ubs_lon: float,
    ordem: list[str],
    family_lookup: dict[str, dict],
    coords_lookup: dict[str, dict],
    status: dict[str, str],
    substitutes: dict[str, str | None],
    route_geom: dict | None,
    route_dur_s: float,
    route_dist_m: float,
) -> str:
    import html as _html

    subst_pids = {v for v in substitutes.values() if v}

    # Mapa Folium isolado (UBS + numerados + polyline da rota).
    print_map = folium.Map(
        location=[ubs_lat, ubs_lon], zoom_start=14, tiles="CartoDB positron",
    )
    folium.Marker(
        location=[ubs_lat, ubs_lon],
        icon=folium.Icon(color="black", icon="plus-sign"),
        popup="UBS",
    ).add_to(print_map)
    for i, pid in enumerate(ordem, start=1):
        c = coords_lookup[pid]
        color = STATUS_COLOR.get(status.get(pid, STATUS_PENDENTE), "#6c757d")
        folium.CircleMarker(
            location=[c["lat"], c["lon"]],
            radius=10, color=color, fill=True, fill_opacity=0.85, weight=2,
        ).add_to(print_map)
        folium.map.Marker(
            [c["lat"], c["lon"]],
            icon=folium.DivIcon(
                icon_size=(20, 20), icon_anchor=(10, 10),
                html=f'<div style="font-size:10px;color:white;text-align:center;font-weight:bold">{i}</div>',
            ),
        ).add_to(print_map)
    if route_geom is not None:
        latlon = [(lat, lon) for lon, lat in route_geom["coordinates"]]
        folium.PolyLine(locations=latlon, color="#4363d8", weight=4, opacity=0.7).add_to(print_map)

    map_html = print_map.get_root().render()
    map_srcdoc = _html.escape(map_html, quote=True)

    rows_html = []
    for i, pid in enumerate(ordem, start=1):
        fam = family_lookup[pid]
        cur_status = status.get(pid, STATUS_PENDENTE)
        color = STATUS_COLOR.get(cur_status, "#6c757d")
        prefix = "🔁 " if pid in subst_pids else ""

        member_items = []
        priority_set = set(fam["priority_member_ids"])
        for m in fam["members"]:
            m_flags = []
            if m.get("gestacao"):
                m_flags.append("gestação")
            if m.get("diabetico"):
                m_flags.append("DM")
            if m.get("hipertenso"):
                m_flags.append("HAS")
            if m.get("vulnerabilidade"):
                m_flags.append(f"vuln:{m['vulnerabilidade']}")
            tag = " <b>★ prioritário</b>" if m["paciente_id"] in priority_set else ""
            sexo = m.get("sexo") or "—"
            faixa = m.get("faixa_etaria") or "—"
            flags_str = f" — {' / '.join(m_flags)}" if m_flags else ""
            member_items.append(
                f"<li><span class='mono'>{_html.escape(str(m['paciente_id'])[:10])}…</span> "
                f"{_html.escape(str(sexo))} · {_html.escape(str(faixa))}{_html.escape(flags_str)}{tag}</li>"
            )
        members_html = "<ul class='members'>" + "".join(member_items) + "</ul>"

        rows_html.append(
            f"<tr>"
            f"<td class='num'>{i:02d}</td>"
            f"<td><b>{prefix}{_html.escape(fam['id_familia_sint'])}</b> "
            f"<span class='muted'>· {fam['member_count']} morador(es) · {fam['priority_band']}</span>"
            f"{members_html}</td>"
            f"<td class='addr'></td>"
            f"<td><span class='badge' style='background:{color}'>{_html.escape(cur_status)}</span></td>"
            f"<td class='chk'>☐</td>"
            f"</tr>"
        )

    duration_label = f"{route_dur_s/3600:.1f}h" if route_dur_s else "—"
    distance_label = f"{route_dist_m/1000:.1f}km" if route_dist_m else "—"
    gerado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Rota — {_html.escape(equipe_id[:8])} · {_html.escape(acs_label)} · {selected_date.isoformat()}</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 11pt; color: #222; margin: 16px; }}
  h1 {{ font-size: 16pt; margin: 0 0 4px; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 12px; }}
  .chip {{ background: #eef; padding: 3px 8px; border-radius: 6px; font-size: 10pt; }}
  iframe.map {{ width: 100%; height: 380px; border: 1px solid #ccc; border-radius: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 10pt; }}
  th, td {{ border: 1px solid #bbb; padding: 5px 7px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
  tr:nth-child(even) td {{ background: #fafafa; }}
  td.num {{ text-align: center; width: 28px; font-weight: bold; }}
  td.mono {{ font-family: ui-monospace, Menlo, monospace; font-size: 9pt; white-space: nowrap; }}
  td.chk {{ text-align: center; font-size: 16pt; width: 28px; }}
  td.addr {{ width: 28%; background: repeating-linear-gradient(to bottom, transparent 0, transparent 16px, #d0d0d0 16px, #d0d0d0 17px); min-height: 80px; }}
  ul.members {{ margin: 4px 0 0 16px; padding: 0; font-size: 9pt; }}
  ul.members li {{ margin: 1px 0; }}
  .badge {{ color: white; padding: 2px 6px; border-radius: 4px; font-size: 9pt; white-space: nowrap; }}
  .muted {{ color: #666; font-size: 9pt; }}
  footer {{ margin-top: 16px; color: #888; font-size: 9pt; }}
  .print-btn {{ position: fixed; top: 10px; right: 10px; padding: 8px 14px; background: #4363d8; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 11pt; }}
  @media print {{
    @page {{ size: A4; margin: 12mm; }}
    .no-print {{ display: none !important; }}
    tr {{ page-break-inside: avoid; }}
    iframe.map {{ height: 320px; }}
  }}
</style></head>
<body>
<button class="print-btn no-print" onclick="window.print()">🖨️ Imprimir</button>
<h1>Rota do dia — {selected_date.isoformat()}</h1>
<div class="chips">
  <span class="chip">Equipe <b>{_html.escape(equipe_id[:8])}</b></span>
  <span class="chip">{_html.escape(acs_label)}</span>
  <span class="chip">{len(ordem)} paradas</span>
  <span class="chip">Duração ≈ {duration_label}</span>
  <span class="chip">Distância ≈ {distance_label}</span>
</div>
<iframe class="map" srcdoc="{map_srcdoc}" sandbox="allow-scripts allow-same-origin"></iframe>
<table>
  <thead><tr>
    <th>#</th><th>Domicílio · membros</th><th>Endereço</th><th>Status (digital)</th><th>☑</th>
  </tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
<footer>
  Gerado em {gerado_em} — Radar Família (protótipo). Coordenadas com ruído de anonimização (~100m).
  Status anotado à mão nesta folha é a versão de campo; o app só persiste o que for re-inserido digitalmente.
</footer>
</body></html>"""


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
    st.markdown("### 📡 Radar Família")
    st.caption("planejamento diário de visitas")
    st.divider()

    equipes = load_equipes()
    pacientes_all = load_pacientes()

    equipes_validas = sorted(
        set(equipes["equipe_id"]) & set(pacientes_all["equipe_id"].unique())
    )
    equipe_id = st.selectbox(
        "Equipe",
        options=equipes_validas,
        format_func=lambda eid: f"{eid[:8]}… ({(pacientes_all['equipe_id'] == eid).sum()} pacientes)",
    )

    with st.expander("⚙️ Ajustes do dia", expanded=False):
        k_clusters = st.number_input(
            "Número de ACS na equipe",
            min_value=1,
            max_value=20,
            value=8,
            help="Como dividir os pacientes em micro-áreas, uma por ACS.",
        )
        n_dia = st.slider(
            "Tamanho da lista do dia",
            min_value=5,
            max_value=25,
            value=12,
            help="Quantas famílias entram na lista priorizada de hoje.",
        )
        st.caption(
            "Priorização atual combina proximidade da UBS, follow-up de quem "
            "não foi atendido e cadência mensal (1 visita/mês por domicílio)."
        )

    pacientes_eq = pacientes_all[pacientes_all["equipe_id"] == equipe_id].reset_index(drop=True)
    pacientes_eq = clusterizar(pacientes_eq, k=int(k_clusters))

    cluster_ids_sorted = sorted(pacientes_eq["cluster_id"].unique())
    acs_label_by_cluster = {cid: f"ACS {idx + 1}" for idx, cid in enumerate(cluster_ids_sorted)}
    cluster_id = st.selectbox(
        "ACS responsável",
        options=cluster_ids_sorted,
        format_func=lambda c: acs_label_by_cluster[c],
    )
    st.caption(
        f"{acs_label_by_cluster[cluster_id]} · "
        f"{(pacientes_eq['cluster_id'] == cluster_id).sum()} pacientes na micro-área"
    )

    if SELECTED_DATE_KEY not in st.session_state:
        st.session_state[SELECTED_DATE_KEY] = TODAY
    selected_date = st.date_input("Dia da visita", key=SELECTED_DATE_KEY)

    with st.expander("🛠️ Diagnóstico", expanded=False):
        if osrm_health():
            st.success(f"OSRM foot disponível ({OSRM_FOOT})")
        else:
            st.error(
                f"OSRM foot indisponível em `{OSRM_FOOT}`.\n\n"
                "Rode: `cd routing && docker compose --profile routing up -d osrm-foot`"
            )
            st.stop()
        if st.button(
            "🔄 Recomeçar dia",
            help="Descarta as marcações deste dia e gera a lista de novo.",
            use_container_width=True,
        ):
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
        month_snaps = snapshots_in_range(
            equipe_id,
            int(cluster_id),
            panel_key,
            month_start(selected_date),
            selected_date - timedelta(days=1),
        )
        monthly_outcomes = family_outcomes_in_range(month_snaps, family_lookup)
        exclude_pids = month_fulfilled_pids(monthly_outcomes)
        forced_front_pids = {
            fid
            for fid, outcome in monthly_outcomes.items()
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
            exclude_pids=exclude_pids,
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
                f"🔁 Substituto encontrado para "
                f"{label_lookup.get(trigger_pid, trigger_pid[:8])}: "
                f"{label_lookup.get(sub_pid, sub_pid[:8])} "
                f"(desvio +{int(detour_m)}m).",
                None,
            ))
        else:
            auto_substitute_changed = True
            auto_substitute_toasts.append((
                f"Nenhum substituto viável no caminho para "
                f"{label_lookup.get(trigger_pid, trigger_pid[:8])} — siga sem substituir.",
                "⚠️",
            ))

    if auto_substitute_changed:
        st.session_state[key_member_status] = member_status
        st.session_state[key_subst] = substitutes
        for message, icon in auto_substitute_toasts:
            st.toast(message, icon=icon)

    read_only = False


# ─── Faixa de contexto (topo) ─────────────────────────────────────────────────
if selected_date < TODAY:
    date_badge = "🔒 dia passado"
elif selected_date > TODAY:
    date_badge = "📅 planejamento futuro"
else:
    date_badge = "📍 hoje"

ctx_cols = st.columns([2, 2, 2, 2, 2])
ctx_cols[0].markdown(
    f"**{selected_date.strftime('%d/%m/%Y')}**  \n<span style='color:#6c757d;font-size:12px'>{date_badge}</span>",
    unsafe_allow_html=True,
)
ctx_cols[1].markdown(
    f"**Equipe**  \n<span style='font-family:monospace;font-size:12px'>{equipe_id[:10]}…</span>",
    unsafe_allow_html=True,
)
ctx_cols[2].markdown(
    f"**{acs_label_by_cluster[cluster_id]}**  \n<span style='color:#6c757d;font-size:12px'>{len(familias_panel)} domicílios na micro-área</span>",
    unsafe_allow_html=True,
)
_n_total = len(ordem)
_n_aten_ctx = sum(1 for s in status.values() if s == STATUS_ATENDIDO)
ctx_cols[3].markdown(
    f"**Progresso**  \n<span style='color:#6c757d;font-size:12px'>{_n_aten_ctx}/{_n_total} atendidos</span>",
    unsafe_allow_html=True,
)
_save_placeholder = ctx_cols[4].empty()
_save_placeholder.markdown(
    "<div style='text-align:right;color:#6c757d;font-size:12px'>—</div>",
    unsafe_allow_html=True,
)
if _n_total:
    st.progress(_n_aten_ctx / _n_total)

with st.expander("ℹ️ Sobre os dados e a rota", expanded=False):
    st.markdown(
        "Coordenadas têm ruído de anonimização (~100m + embaralhamento por equipe). "
        "Rotas e durações são metodológicas, não operacionais — OSM costuma mapear "
        "mal vielas e escadarias em comunidades. A rota segue a ordem definida pelo "
        "ACS (sem reotimização de paradas)."
    )

# ─── Layout ───────────────────────────────────────────────────────────────────
col_lista, col_mapa = st.columns([65, 35])

with col_lista:
    # Métricas do dia (acima da lista)
    n_pend = sum(1 for s in status.values() if s == STATUS_PENDENTE)
    n_aten = sum(1 for s in status.values() if s == STATUS_ATENDIDO)
    n_parcial = sum(1 for s in status.values() if s == STATUS_PARCIAL)
    n_no = sum(1 for s in status.values() if s == STATUS_NO_SHOW)
    n_rec = sum(1 for s in status.values() if s == STATUS_RECUSOU)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Pendente", n_pend)
    m2.metric("Atendeu", n_aten)
    m3.metric("Parcial", n_parcial)
    m4.metric("Não atendeu", n_no)
    m5.metric("Recusou", n_rec)

    header = "Domicílios do dia"
    if read_only:
        header += " · 🔒 somente leitura"
    st.subheader(header)
    if not read_only:
        st.caption(
            "Selecione o status de cada morador no dropdown: pendente, atendeu, "
            "não atendeu ou recusou."
        )

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
                    f"♻️ {n_followup_prev} domicílio(s) re-agendado(s) — "
                    f"não foram atendidos em {prev['exported_at'][:10]}."
                )
        month_snaps = snapshots_in_range(
            equipe_id,
            int(cluster_id),
            panel_key,
            month_start(selected_date),
            selected_date - timedelta(days=1),
        )
        monthly_outcomes = family_outcomes_in_range(month_snaps, family_lookup)
        excluded_month = month_fulfilled_pids(monthly_outcomes)
        if excluded_month:
            st.caption(
                f"🗓️ {len(excluded_month)} domicílio(s) já atendido(s) neste mês "
                "foram excluído(s) do panel do dia."
            )
        forced_same_month = {
            fid
            for fid, outcome in monthly_outcomes.items()
            if family_lookup.get(fid, {}).get("priority_band") == FAMILY_PRIORITY_HIGH
            and (
                outcome["status"] == STATUS_NO_SHOW
                or outcome.get("has_partial_priority_gap")
            )
            and outcome.get("date")
            and date.fromisoformat(outcome["date"]) <= (selected_date - timedelta(days=1))
        }
        if forced_same_month:
            st.caption(
                f"🚨 {len(forced_same_month)} domicílio(s) de prioridade alta com "
                "morador pendente foram puxado(s) para este mês."
            )

    PRIORITY_COLORS = {
        FAMILY_PRIORITY_HIGH: "#e74c3c",
        FAMILY_PRIORITY_MEDIUM: "#f39c12",
        FAMILY_PRIORITY_LOW: "#6c757d",
    }
    CHIP_NEUTRAL = "#eef0f3"
    CHIP_TEXT = "#374151"

    def _chip(text: str, bg: str = CHIP_NEUTRAL, fg: str = CHIP_TEXT) -> str:
        return (
            f"<span style='background:{bg};color:{fg};padding:3px 10px;"
            f"border-radius:999px;font-size:13px;margin-right:6px;"
            f"display:inline-block;white-space:nowrap;line-height:1.6'>{text}</span>"
        )

    def _status_pill(s: str, large: bool = False) -> str:
        pad = "6px 12px" if large else "3px 10px"
        size = "14px" if large else "13px"
        return (
            f"<span style='background:{STATUS_COLOR[s]};color:white;"
            f"padding:{pad};border-radius:999px;font-size:{size};"
            f"font-weight:500;white-space:nowrap'>{s}</span>"
        )

    def _family_chips(family: dict) -> str:
        chips = []
        pb = family["priority_band"]
        chips.append(_chip(f"prioridade {pb}", bg=PRIORITY_COLORS[pb], fg="white"))
        chips.append(_chip(f"👥 {family['member_count']}"))
        if any(m["gestacao"] for m in family["members"]):
            chips.append(_chip("gestação"))
        if any(m["diabetico"] for m in family["members"]):
            chips.append(_chip("DM"))
        if any(m["hipertenso"] for m in family["members"]):
            chips.append(_chip("HAS"))
        if any(m["vulnerabilidade"] for m in family["members"]):
            chips.append(_chip("vulnerab."))
        return "".join(chips)

    last_idx = len(ordem) - 1
    subst_pids = {v for v in substitutes.values() if v}
    for i, pid in enumerate(ordem):
        cur_status = status.get(pid, STATUS_PENDENTE)
        family = family_lookup[pid]
        is_subst = pid in subst_pids
        with st.container(border=True):
            head = st.columns([1, 7, 3])
            with head[0]:
                st.markdown(
                    f"<div style='font-size:22px;font-weight:700;color:#111;line-height:1.4'>"
                    f"{i+1:02d}</div>",
                    unsafe_allow_html=True,
                )
            with head[1]:
                subst_tag = (
                    "<span style='background:#fff3cd;color:#856404;padding:2px 8px;"
                    "border-radius:4px;font-size:11px;margin-right:6px;font-weight:600'>"
                    "SUBSTITUTO</span>"
                    if is_subst else ""
                )
                st.markdown(
                    f"<div style='font-family:ui-monospace,monospace;font-size:15px;"
                    f"color:#111;font-weight:600'>{subst_tag}"
                    f"{family['id_familia_sint'][:14]}…</div>"
                    f"<div style='margin-top:8px'>{_family_chips(family)}</div>",
                    unsafe_allow_html=True,
                )
            with head[2]:
                st.markdown(
                    f"<div style='text-align:right'>{_status_pill(cur_status, large=True)}</div>",
                    unsafe_allow_html=True,
                )

            if not read_only:
                key_suffix = f"{equipe_id}_{cluster_id}_{selected_date.isoformat()}_{pid}"
                ctrl = st.columns([1, 1, 8])
                if ctrl[0].button("⬆︎", key=f"up_{key_suffix}", disabled=(i == 0),
                                  use_container_width=True, help="Subir na rota"):
                    new_ordem = list(ordem)
                    new_ordem[i - 1], new_ordem[i] = new_ordem[i], new_ordem[i - 1]
                    st.session_state[key_ordem] = new_ordem
                    st.rerun()
                if ctrl[1].button("⬇︎", key=f"dn_{key_suffix}", disabled=(i == last_idx),
                                  use_container_width=True, help="Descer na rota"):
                    new_ordem = list(ordem)
                    new_ordem[i + 1], new_ordem[i] = new_ordem[i], new_ordem[i + 1]
                    st.session_state[key_ordem] = new_ordem
                    st.rerun()
            else:
                key_suffix = f"{equipe_id}_{cluster_id}_{selected_date.isoformat()}_{pid}"

            with st.expander("Ver moradores e histórico"):
                stat_cols = st.columns(2)
                with stat_cols[0]:
                    extra = (
                        f"<div style='color:#6c757d;font-size:12px;margin-top:2px'>última {family['ultima_visita']}</div>"
                        if family['ultima_visita'] else ""
                    )
                    st.markdown(
                        f"<div style='font-size:13px;color:#6c757d;text-transform:uppercase;"
                        f"letter-spacing:0.5px;font-weight:600'>Visitas anteriores</div>"
                        f"<div style='font-size:24px;font-weight:700;color:#111;line-height:1.2'>"
                        f"{family['n_visitas_total']}</div>{extra}",
                        unsafe_allow_html=True,
                    )
                with stat_cols[1]:
                    extra = (
                        f"<div style='color:#6c757d;font-size:12px;margin-top:2px'>último {family['ultimo_evento']}</div>"
                        if family['ultimo_evento'] else ""
                    )
                    st.markdown(
                        f"<div style='font-size:13px;color:#6c757d;text-transform:uppercase;"
                        f"letter-spacing:0.5px;font-weight:600'>Eventos clínicos</div>"
                        f"<div style='font-size:24px;font-weight:700;color:#111;line-height:1.2'>"
                        f"{family['n_eventos_total']}</div>{extra}",
                        unsafe_allow_html=True,
                    )

                n_priority = len(family["priority_member_ids"])
                priority_note = (
                    f" · <span style='color:#e74c3c;font-weight:600'>{n_priority} prioritário(s)</span>"
                    if n_priority else ""
                )
                st.markdown(
                    f"<div style='margin-top:18px;font-size:13px;color:#6c757d;"
                    f"text-transform:uppercase;letter-spacing:0.5px;font-weight:600'>"
                    f"Moradores ({family['member_count']}){priority_note}</div>",
                    unsafe_allow_html=True,
                )

                for member in family["members"]:
                    member_pid = member["paciente_id"]
                    member_cur_status = member_status[pid][member_pid]
                    is_priority = member_pid in family["priority_member_ids"]
                    cond_chips = []
                    if member["gestacao"]:
                        cond_chips.append(_chip("gestação"))
                    if member["diabetico"]:
                        cond_chips.append(_chip("DM"))
                    if member["hipertenso"]:
                        cond_chips.append(_chip("HAS"))
                    vuln = member.get("vulnerabilidade")
                    if vuln:
                        vuln_text = (
                            str(vuln)
                            if isinstance(vuln, str) and vuln.lower() not in {"true", "false"}
                            else "vulnerab."
                        )
                        cond_chips.append(_chip(vuln_text))
                    chips_html = "".join(cond_chips)

                    star = (
                        "<span title='morador prioritário' style='color:#e74c3c;"
                        "font-weight:700;margin-right:6px;font-size:16px'>★</span>"
                        if is_priority else ""
                    )
                    sexo = member.get("sexo") or "—"
                    faixa = member.get("faixa_etaria") or "—"

                    history_bits = []
                    if member["n_visitas"]:
                        b = f"📋 {member['n_visitas']} visita(s)"
                        if member["ultima_visita"]:
                            b += f" · {member['ultima_visita']}"
                        history_bits.append(b)
                    if member["n_eventos"]:
                        b = f"⚕️ {member['n_eventos']} evento(s)"
                        if member["ultimo_evento"]:
                            b += f" · {member['ultimo_evento']}"
                        history_bits.append(b)
                    history_html = (
                        f"<div style='font-size:13px;color:#6c757d;margin-top:6px'>"
                        f"{' · '.join(history_bits)}</div>"
                        if history_bits else ""
                    )

                    with st.container(border=True):
                        mcols = st.columns([7, 3])
                        with mcols[0]:
                            st.markdown(
                                f"<div style='font-size:15px;color:#111;line-height:1.4'>"
                                f"{star}<b>{sexo}</b> · {faixa}"
                                f"<span style='color:#9ca3af;font-family:ui-monospace,monospace;"
                                f"font-size:12px;margin-left:8px'>{member_pid[:10]}…</span>"
                                f"</div>"
                                + (f"<div style='margin-top:6px'>{chips_html}</div>" if chips_html else "")
                                + history_html,
                                unsafe_allow_html=True,
                            )
                        with mcols[1]:
                            if read_only:
                                st.markdown(
                                    f"<div style='text-align:right;padding-top:8px'>"
                                    f"{_status_pill(member_cur_status)}</div>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                new_choice = st.selectbox(
                                    "status",
                                    options=STATUS_CYCLE,
                                    index=STATUS_CYCLE.index(member_cur_status),
                                    key=f"member_{key_suffix}_{member_pid}",
                                    label_visibility="collapsed",
                                    help="Selecione o status deste morador",
                                )
                                if new_choice != member_cur_status:
                                    member_status[pid][member_pid] = new_choice
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

# ─── Export + duração ─────────────────────────────────────────────────────────
with col_lista:
    st.divider()
    duration_label = f"{route_dur_s/3600:.1f}h" if route_dur_s else "—"
    distance_label = f"{route_dist_m/1000:.1f}km" if route_dist_m else "—"
    st.caption(f"⏱️ Duração estimada da rota: **{duration_label}** · {distance_label}")

    if ordem:
        print_html = build_print_html(
            equipe_id=equipe_id,
            cluster_id=int(cluster_id),
            acs_label=acs_label_by_cluster.get(cluster_id, f"ACS {cluster_id}"),
            selected_date=selected_date,
            ubs_lat=float(ubs["ubs_lat"]),
            ubs_lon=float(ubs["ubs_lon"]),
            ordem=ordem,
            family_lookup=family_lookup,
            coords_lookup=coords_lookup,
            status=status,
            substitutes=substitutes,
            route_geom=route_geom,
            route_dur_s=route_dur_s,
            route_dist_m=route_dist_m,
        )
        st.download_button(
            "🖨️ Baixar rota imprimível (HTML)",
            data=print_html,
            file_name=f"rota_{equipe_id[:8]}_acs{int(cluster_id)}_{selected_date.isoformat()}.html",
            mime="text/html",
            use_container_width=True,
        )

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
                "top-N domicílios por centróide + rerank no-show + cadência mensal (exclusão dos atendidos no mês)"
            ),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "ordem": ordem,
            "status": status,
            "member_status": member_status,
            "substitutes": substitutes,
            "rota": {"duration_s": route_dur_s, "distance_m": route_dist_m},
        }
        try:
            out = save_snapshot(payload, equipe_id, int(cluster_id), panel_key, selected_date)
            _save_placeholder.markdown(
                f"<div style='text-align:right;color:#2ecc71;font-size:12px'>"
                f"✓ Salvo {datetime.now().strftime('%H:%M:%S')}</div>",
                unsafe_allow_html=True,
            )
        except Exception:
            _save_placeholder.markdown(
                "<div style='text-align:right;color:#e74c3c;font-size:12px'>⚠️ Não salvo</div>",
                unsafe_allow_html=True,
            )
            raise

# ─── Mapa ─────────────────────────────────────────────────────────────────────
with col_mapa:
    st.subheader("Rota no mapa")
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
            tooltip=f"{i:02d}. {label_lookup[pid]} — {status.get(pid)}",
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
            tooltip=(
                f"Duração ≈ {route_dur_s/3600:.1f}h · "
                f"{route_dist_m/1000:.1f}km (estimativa OSRM)"
            ),
        ).add_to(m)

    st_folium(m, height=720, use_container_width=True, returned_objects=[])

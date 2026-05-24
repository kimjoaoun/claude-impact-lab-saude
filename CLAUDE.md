# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Nature

This repository is no longer documentation-only. It now contains:

- a `Streamlit` application in `app/` for planning daily ACS home visits;
- a self-hosted `OSRM` routing setup in `routing/`;
- supporting documentation in `README.md`;
- exploratory notebooks in `notebooks/`.

There is still no formal test suite in the repo.

## Product Context

The challenge is "Inteligência no Território": recommend a prioritized daily home-visit list for ACS, including:

- who to visit;
- in what order;
- why they were prioritized.

The current app is a planning prototype. It lets the user:

- choose an `equipe_id`;
- split patients into geographic clusters that stand in for ACS panels;
- generate a day list with a placeholder heuristic;
- reorder the visit sequence manually;
- mark each visit outcome;
- draw the route on a map using OSRM;
- persist the day state as JSON snapshots.

## Current Code Layout

- `app/streamlit_app.py`: main UI and planning flow.
- `app/lib/data.py`: Parquet loading, bbox filtering, clustering, selection heuristics, substitute search.
- `app/lib/osrm.py`: OSRM health check and fixed-waypoint route requests.
- `app/lib/state.py`: snapshot persistence in `app/data/dia/`.
- `routing/docker-compose.yml`: local OSRM services (`foot` and `car`).
- `routing/scripts/`: OSM download, crop, and OSRM build scripts.

## Data Dependencies

The source data is not committed to the repo. The app expects these Parquet files in `~/Documents`:

- `equipes_anonimizadas.parquet`
- `pacientes_anonimizados.parquet`
- `visitas_anonimizadas.parquet`
- `eventos_clinicos_anonimizados.parquet`

Do not commit raw data or derived sensitive exports to the repository.

## Dataset Context

The data has been heavily anonymized. Important consequences:

- absolute indicators do not reflect the real world;
- only relative dynamics, event ordering, and territorial logic are intended to survive;
- coordinates include geographic noise and address shuffling within team territory;
- patient-level dates are shifted while preserving intra-patient order.

When analyzing results or changing ranking logic, do not present aggregates as operational truth. Treat them as methodological signals for prototyping.

## Routing Context

The app depends on OSRM for route drawing and duration estimation.

- Default endpoint: `OSRM_FOOT=http://localhost:5050`
- Health check is required at app startup.
- The route is computed with fixed waypoint order via `/route`; the UI does not ask OSRM to optimize stop order.

Important limitation: OSM coverage in communities can be incomplete, so routes are approximate and should not be treated as field-operational instructions.

## Working In This Repo

- `README.md` is in Portuguese (`pt-BR`). Match that language when editing it.
- Preserve the README's current style: badges, emoji-prefixed sections, and Mermaid diagrams.
- Prefer small, surgical edits. There is meaningful prototype logic in `app/` even though the heuristic is still placeholder-grade.
- If you change persistence or planning semantics, inspect both `app/streamlit_app.py` and `app/lib/state.py`; they are tightly coupled.
- If you change routing assumptions, inspect both `app/lib/osrm.py` and `routing/README.md`.

## Practical Notes

- The Streamlit app appears to be run from `app/`.
- Snapshot files are stored under `app/data/dia/`.
- There is an `app/.venv/` in the workspace; avoid noisy commands that recurse through it unless needed.
- `streamlit-sortables` is listed as a dependency but is not currently used by the main app.

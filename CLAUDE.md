# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Nature

This is a **documentation-only repository** for the Claude Impact Lab 2026 — Dataset Saúde do Rio challenge. There is no source code, build system, or test suite. The repo currently contains only `README.md`, which documents an anonymized public-health dataset published by the Prefeitura do Rio de Janeiro.

The actual data (Parquet files for `equipes`, `pacientes`, `visitas`, `eventos_clinicos`) lives on Google Drive — links are in the README. The data is not in-repo and should not be committed here.

## Dataset Context (important for any analysis the user may request)

- The data has been heavily anonymized (SHA256 hashing, ~100m geographic noise, per-patient random date shifting, address shuffling within team territory, k-anonymity ≥5 suppression, sampling of 2,000 patients per team).
- **Absolute indicators derived from this data do not reflect reality.** Only relative dynamics, event ordering, and territorial logic (with noise) are preserved.
- When helping the user analyze or model this data, surface this caveat rather than presenting raw aggregates as real-world findings.

## Data Model

Relationships (see ER diagram in README.md):
- `equipes` 1—N `pacientes` (via `equipe_id`)
- `pacientes` 1—N `visitas` (via `paciente_id`)
- `pacientes` 1—N `eventos_clinicos` (via `paciente_id`)

The challenge ("Inteligência no Território") is to use this data to recommend a prioritized daily home-visit list for each of the ~6,200 ACS (Agentes Comunitários de Saúde) — i.e. *who* to visit, *in what order*, and *why*, based on risk and care gaps. ACS routes start from the team's `endereco_lat/lon` (the clinic location).

## Working in this repo

- README.md is in Portuguese (pt-BR). Match that language when editing it.
- The README uses Mermaid ER diagrams, badge shields, and emoji-prefixed section headers — preserve that style when editing.

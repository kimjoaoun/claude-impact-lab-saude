#!/usr/bin/env bash
set -euo pipefail

# Helper compartilhado: roda o pipeline MLD do OSRM para um perfil.
# Uso: _build-profile.sh <profile>     (profile = foot | car | bicycle | ...)
#
# Gera os artefatos em routing/data/<profile>/rio.osrm.*
# Usa a mesma imagem do compose, mas via `docker run` (sem subir servico).

PROFILE="${1:?uso: $0 <profile>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$(cd "${SCRIPT_DIR}/../data" && pwd)"
PBF="${DATA_DIR}/rio-crop.osm.pbf"
PROFILE_DIR="${DATA_DIR}/${PROFILE}"
IMAGE="ghcr.io/project-osrm/osrm-backend:latest"

if [[ ! -f "${PBF}" ]]; then
  echo "Erro: ${PBF} nao encontrado." >&2
  echo "Rode scripts/download-osm.sh e depois scripts/crop-osm.sh primeiro." >&2
  exit 1
fi

mkdir -p "${PROFILE_DIR}"
# osrm-extract gera os arquivos no mesmo diretorio do PBF de entrada,
# entao copiamos para o diretorio do profile.
cp -f "${PBF}" "${PROFILE_DIR}/rio.osm.pbf"

run_osrm() {
  docker run --rm -t \
    -v "${DATA_DIR}:/data" \
    "${IMAGE}" \
    "$@"
}

echo "==> osrm-extract (${PROFILE})"
run_osrm osrm-extract -p "/opt/${PROFILE}.lua" "/data/${PROFILE}/rio.osm.pbf"

echo "==> osrm-partition (${PROFILE})"
run_osrm osrm-partition "/data/${PROFILE}/rio.osrm"

echo "==> osrm-customize (${PROFILE})"
run_osrm osrm-customize "/data/${PROFILE}/rio.osrm"

# PBF intermediario do profile nao e mais necessario apos o extract.
rm -f "${PROFILE_DIR}/rio.osm.pbf"

echo "Pronto. Artefatos em ${PROFILE_DIR}/"

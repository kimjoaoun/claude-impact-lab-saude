#!/usr/bin/env bash
set -euo pipefail

# Baixa o extrato OSM da regiao Sudeste do Brasil (Geofabrik).
# Geofabrik descontinuou o extrato por estado (rio-de-janeiro-latest);
# o menor disponivel que cobre o Rio agora e sudeste (~500MB).
# O bbox crop em crop-osm.sh reduz isso a ~10-20MB pra a area de cobertura.
#
# Override opcional via env var OSM_URL.

OSM_URL="${OSM_URL:-https://download.geofabrik.de/south-america/brazil/sudeste-latest.osm.pbf}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$(cd "${SCRIPT_DIR}/../data" && pwd)"
TARGET="${DATA_DIR}/rio.osm.pbf"

if [[ -f "${TARGET}" ]]; then
  echo "PBF ja existe em ${TARGET} — pulando download."
  echo "Para forcar novo download, apague o arquivo e rode de novo."
  exit 0
fi

echo "Baixando ${OSM_URL}"
echo "  -> ${TARGET}"
curl -fL --progress-bar -o "${TARGET}" "${OSM_URL}"
echo "Pronto. $(du -h "${TARGET}" | cut -f1) baixados."

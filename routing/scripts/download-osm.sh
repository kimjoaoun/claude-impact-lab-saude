#!/usr/bin/env bash
set -euo pipefail

# Baixa o extrato OSM do estado do Rio de Janeiro (Geofabrik).
# Geofabrik nao publica extrato a nivel de municipio; o estado de RJ
# (~150MB) cobre cidade + regiao metropolitana com folga.
#
# Override opcional via env var OSM_URL para usar outro extrato
# (ex.: sudeste-latest.osm.pbf se algum dia o estado nao bastar).

OSM_URL="${OSM_URL:-https://download.geofabrik.de/south-america/brazil/sudeste/rio-de-janeiro-latest.osm.pbf}"

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

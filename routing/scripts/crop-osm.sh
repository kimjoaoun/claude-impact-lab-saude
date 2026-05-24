#!/usr/bin/env bash
set -euo pipefail

# Recorta o PBF do estado do Rio para a area de cobertura do desafio
# (zona norte: Tijuca / Maracana / Andarai / Vila Isabel / Grajau / Rio Comprido).
#
# Bbox default = bbox das 9 UBS + ~3 km de buffer, suficiente pra cobrir
# todos os pacientes nao-outliers (ruido de anonimizacao ~100 m + shuffle
# de endereco dentro do territorio da equipe).
#
# Override via env: LON_MIN LAT_MIN LON_MAX LAT_MAX.

LON_MIN="${LON_MIN:--43.310}"
LAT_MIN="${LAT_MIN:--22.995}"
LON_MAX="${LON_MAX:--43.180}"
LAT_MAX="${LAT_MAX:--22.885}"

IMAGE="${OSMIUM_IMAGE:-ghcr.io/osmcode/osmium-tool:latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$(cd "${SCRIPT_DIR}/../data" && pwd)"
SRC="${DATA_DIR}/rio.osm.pbf"
DST="${DATA_DIR}/rio-crop.osm.pbf"

if [[ ! -f "${SRC}" ]]; then
  echo "Erro: ${SRC} nao encontrado. Rode scripts/download-osm.sh primeiro." >&2
  exit 1
fi

if [[ -f "${DST}" && "${DST}" -nt "${SRC}" ]]; then
  echo "Crop ja existe e e mais novo que o PBF de origem — pulando."
  echo "  ${DST}"
  echo "Para forcar, apague o arquivo e rode de novo."
  exit 0
fi

echo "Recortando ${SRC}"
echo "  bbox = ${LON_MIN},${LAT_MIN},${LON_MAX},${LAT_MAX}"
echo "  -> ${DST}"

# Prefere osmium local (brew install osmium-tool); cai pra Docker se nao tiver.
if command -v osmium >/dev/null 2>&1; then
  osmium extract \
    --bbox "${LON_MIN},${LAT_MIN},${LON_MAX},${LAT_MAX}" \
    --overwrite \
    -o "${DST}" \
    "${SRC}"
else
  docker run --rm -t \
    -v "${DATA_DIR}:/data" \
    "${IMAGE}" \
    osmium extract \
      --bbox "${LON_MIN},${LAT_MIN},${LON_MAX},${LAT_MAX}" \
      --overwrite \
      -o /data/rio-crop.osm.pbf \
      /data/rio.osm.pbf
fi

echo "Pronto. $(du -h "${DST}" | cut -f1) ($(du -h "${SRC}" | cut -f1) antes do crop)."

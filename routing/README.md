# Routing (OSRM)

Motor de cálculo de rotas self-hosted para a etapa de planejamento de visitas dos ACS. Roda dois perfis em paralelo:

- **foot** (porta `5000`) — caso padrão, ACS a pé no território.
- **car** (porta `5001`) — deslocamentos motorizados / supervisão / comparação.

Usa [OSRM](https://project-osrm.org/) com algoritmo MLD, dados do OpenStreetMap (extrato do estado do Rio de Janeiro via Geofabrik).

## Pré-requisitos

- Docker + Docker Compose
- ~2GB de espaço em disco (PBF + artefatos dos dois perfis)

## Setup (rodado uma vez)

```bash
cd routing
./scripts/download-osm.sh    # ~150MB, baixa rio-de-janeiro-latest.osm.pbf
./scripts/build-foot.sh      # extract + partition + customize (perfil foot)
./scripts/build-car.sh       # idem (perfil car)
```

Cada build leva ~2-3min. Os artefatos ficam em `data/foot/` e `data/car/` (gitignored).

## Subir os serviços

```bash
docker compose --profile routing up -d
```

O `--profile routing` é necessário — sem ele os serviços não sobem (proteção contra `docker compose up` acidental sem ter buildado).

## Endpoints

- **Foot:** `http://localhost:5000/{service}/v1/foot/...`
- **Car:** `http://localhost:5001/{service}/v1/driving/...`

Serviços úteis para este projeto:

- `/route` — rota A→B.
- `/table` — matriz de distância/tempo entre N pontos (input para solver TSP externo).
- `/trip` — resolve TSP direto (heurística), com `source=first` para fixar a sede da equipe como ponto de partida.

### Exemplo: rota a pé partindo da sede da equipe

```bash
curl "http://localhost:5000/trip/v1/foot/\
-43.180,-22.900;-43.185,-22.905;-43.190,-22.902;-43.195,-22.908\
?source=first&roundtrip=false"
```

Retorna `code: "Ok"` e a ordem otimizada das paradas em `waypoints[].waypoint_index`.

## Caveat de cobertura

OSM mapeia bem ruas urbanas formais, mas **vielas, escadarias e passagens em comunidades são frequentemente incompletas**. Para o caso real do ACS (que opera majoritariamente nesses territórios), rotas devem ser tratadas como sugestão metodológica — não como instrução operacional. Vale ter fallback para distância haversine quando OSRM retorna rota visivelmente absurda.

## Arquivos

```
routing/
├── docker-compose.yml         # servicos osrm-foot e osrm-car
├── scripts/
│   ├── download-osm.sh        # baixa PBF do Geofabrik
│   ├── _build-profile.sh      # helper: pipeline MLD generico
│   ├── build-foot.sh          # build do perfil foot
│   └── build-car.sh           # build do perfil car
└── data/                      # PBF + artefatos .osrm.* (gitignored)
```

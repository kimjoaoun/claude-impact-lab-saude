from __future__ import annotations

import os

import requests

OSRM_FOOT = os.environ.get("OSRM_FOOT", "http://localhost:5050")


class OSRMError(RuntimeError):
    pass


def osrm_health() -> bool:
    try:
        r = requests.get(
            f"{OSRM_FOOT}/route/v1/foot/-43.234,-22.928;-43.252,-22.913?overview=false",
            timeout=5,
        )
        r.raise_for_status()
        return r.json().get("code") == "Ok"
    except Exception:
        return False


def osrm_route(coords: list[tuple[float, float]]) -> dict:
    """
    coords: lista [(lon, lat), ...] na ordem desejada. /route honra a ordem.
    Retorna dict com keys: geometry (GeoJSON), duration (s), distance (m), code.
    """
    if len(coords) < 2:
        raise OSRMError("precisa de pelo menos 2 waypoints")
    coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = f"{OSRM_FOOT}/route/v1/foot/{coord_str}"
    params = {"geometries": "geojson", "overview": "full"}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok":
        raise OSRMError(f"OSRM code={data.get('code')} message={data.get('message')}")
    route = data["routes"][0]
    return {
        "geometry": route["geometry"],
        "duration": route["duration"],
        "distance": route["distance"],
        "code": "Ok",
    }

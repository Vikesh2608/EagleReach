import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

APP_VERSION = "1.0.1"
HTTP_TIMEOUT = 12.0

# ---------- FastAPI & CORS ----------
app = FastAPI(title="EagleReach API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your GitHub Pages origin if you prefer
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Small TTL cache ----------
CACHE: Dict[str, Dict[str, Any]] = {}
DEFAULT_TTL = 60 * 10  # 10 minutes

def cache_get(key: str) -> Optional[Any]:
    rec = CACHE.get(key)
    if not rec:
        return None
    if time.time() > rec["exp"]:
        CACHE.pop(key, None)
        return None
    return rec["val"]

def cache_set(key: str, val: Any, ttl: int = DEFAULT_TTL) -> None:
    CACHE[key] = {"val": val, "exp": time.time() + ttl}

# ---------- HTTP helpers ----------
UA = {"User-Agent": "EagleReach/1.0 (+mailto:vikebairam@gmail.com)"}

async def fetch_json(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
    hdrs = {**UA, **(headers or {})}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=hdrs)
        r.raise_for_status()
        # Some civic endpoints return application/text with JSON inside.
        # Try JSON first; if it fails, fall back to manual parse.
        try:
            return r.json()
        except Exception:
            import json
            return json.loads(r.text)

# ---------- Weather code → text (Open-Meteo) ----------
WX_MAP = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm (hail)", 99: "Thunderstorm (hail)",
}

# ---------- Health ----------
@app.get("/health")
async def health():
    return {"ok": True, "service": "eaglereach-api", "version": APP_VERSION}

# ---------- ZIP info (Zippopotam) ----------
@app.get("/zipinfo")
async def zipinfo(zip: str = Query(..., description="US ZIP code")):
    key = f"zipinfo:{zip}"
    cached = cache_get(key)
    if cached:
        return cached

    url = f"https://api.zippopotam.us/us/{zip}"
    try:
        data = await fetch_json(url)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ZIP lookup failed: {e}") from e

    if not data or "places" not in data or not data["places"]:
        raise HTTPException(status_code=404, detail="ZIP not found")

    place = data["places"][0]
    payload = {
        "zip": data.get("post code") or zip,
        "place": place.get("place name"),
        "state": place.get("state"),
        "state_abbr": place.get("state abbreviation"),
        "lat": float(place.get("latitude")) if place.get("latitude") else None,
        "lon": float(place.get("longitude")) if place.get("longitude") else None,
    }
    cache_set(key, payload, ttl=60 * 60)  # 1 hour
    return payload

# ---------- Reverse ZIP from lat/lon (BigDataCloud) ----------
@app.get("/reverse-zip")
async def reverse_zip(lat: float, lon: float):
    key = f"revzip:{lat:.4f},{lon:.4f}"
    cached = cache_get(key)
    if cached:
        return cached

    url = "https://api.bigdatacloud.net/data/reverse-geocode-client"
    params = {"latitude": lat, "longitude": lon, "localityLanguage": "en"}
    try:
        data = await fetch_json(url, params=params)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Reverse geocode failed: {e}") from e

    # Postcode may not be present in rural areas; return what we have.
    zip_code = data.get("postcode") or None
    payload = {
        "zip": zip_code,
        "place": data.get("city") or data.get("locality") or data.get("principalSubdivision"),
        "state": data.get("principalSubdivision"),
        "lat": lat,
        "lon": lon,
    }
    cache_set(key, payload, ttl=60 * 30)
    return payload

# ---------- Elected officials ----------
# Primary: whoismyrepresentative.com (ZIP → House + Senators)
# Fallback: GovTrack (state senators) when ZIP source is unavailable/rate-limited
def _fmt_official(name: str, office: str, party: Optional[str] = None,
                  phone: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    out = {"name": name, "office": office}
    if party:
        out["party"] = party
    if phone:
        out["phones"] = [phone]
    if url:
        out["urls"] = [url]
    return out

@app.get("/officials")
async def officials(zip: str):
    key = f"officials:{zip}"
    cached = cache_get(key)
    if cached:
        return cached

    officials: List[Dict[str, Any]] = []

    # Try WIMR first (returns House + Senate for the ZIP)
    try:
        wimr = await fetch_json(
            "https://whoismyrepresentative.com/getall_mems.php",
            params={"zip": zip, "output": "json"},
            headers={"Accept": "application/json"}
        )
        results = wimr.get("results") or []
        for r in results:
            party = {"D": "Democrat", "R": "Republican"}.get(r.get("party", "").strip(), r.get("party"))
            officials.append(_fmt_official(
                name=r.get("name", "Unknown"),
                office=r.get("office") or r.get("district") or "Representative",
                party=party,
                phone=r.get("phone"),
                url=r.get("link")
            ))
    except Exception:
        # ignore; fall back to GovTrack
        pass

    # Fallback: senators via GovTrack
    if not officials:
        try:
            z = await zipinfo(zip)
            abbr = (z.get("state_abbr") or "").upper()
            if abbr:
                gt = await fetch_json(
                    "https://www.govtrack.us/api/v2/role",
                    params={"current": "true", "role_type": "senator", "state": abbr}
                )
                for role in gt.get("objects", []):
                    person = role.get("person", {})
                    officials.append(_fmt_official(
                        name=f"{person.get('firstname', '')} {person.get('lastname', '')}".strip(),
                        office="United States Senator",
                        party=role.get("party"),
                        phone=role.get("phone"),
                        url=person.get("link")
                    ))
        except Exception:
            pass

    # Helpful Mayor link (best effort) so there is always a city-level entry
    try:
        z = await zipinfo(zip)
        place = z.get("place") or ""
        state = z.get("state_abbr") or z.get("state") or ""
        if place:
            officials.append(_fmt_official(
                name=f"{place} Mayor",
                office="Mayor",
                url=f"https://www.google.com/search?q={place.replace(' ', '+')}+{state}+mayor+official+site"
            ))
    except Exception:
        pass

    payload = {"count": len(officials), "officials": officials}
    cache_set(key, payload, ttl=60 * 30)
    return payload

# ---------- Weather (Open-Meteo daily) ----------
@app.get("/weather")
async def weather(zip: str):
    key = f"wx:{zip}"
    cached = cache_get(key)
    if cached:
        return cached

    z = await zipinfo(zip)
    lat, lon = z.get("lat"), z.get("lon")
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="No lat/lon for ZIP")

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,weathercode",
        "timezone": "auto"
    }
    try:
        data = await fetch_json(url, params=params)
        daily = data.get("daily") or {}
        days = []
        for i, date in enumerate(daily.get("time", [])):
            code = int((daily.get("weathercode") or [0])[i])
            tmax = (daily.get("temperature_2m_max") or [None])[i]
            tmin = (daily.get("temperature_2m_min") or [None])[i]
            days.append({
                "date": date,
                "summary": WX_MAP.get(code, "Weather"),
                "max": tmax,
                "min": tmin,
            })
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Weather fetch failed: {e}") from e

    payload = {"source": "open-meteo", "location": {"lat": lat, "lon": lon}, "days": days}
    cache_set(key, payload, ttl=60 * 30)
    return payload

# ---------- Elections (simple helper text) ----------
def next_federal_election() -> str:
    # Helper text for the next federal general—adjust as new cycle approaches.
    return "November 3, 2025"

@app.get("/elections")
async def elections(zip: str):
    return {"next": {"federal": next_federal_election()}}

# ---------- City Updates (demo: NYC & Chicago open data) ----------
@app.get("/city/updates")
async def city_updates(zip: str):
    key = f"city:{zip}"
    cached = cache_get(key)
    if cached:
        return cached

    z = await zipinfo(zip)
    place = (z.get("place") or "").lower()
    state = (z.get("state_abbr") or z.get("state") or "").upper()

    items: List[Dict[str, Any]] = []
    try:
        if "new york" in place and state == "NY":
            url = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
            params = {"$limit": 10, "incident_zip": zip}
            data = await fetch_json(url, params=params)
            for r in data:
                items.append({
                    "type": r.get("complaint_type") or "311",
                    "detail": r.get("descriptor") or r.get("incident_address") or "Issue",
                    "status": r.get("status") or "",
                    "created": r.get("created_date") or "",
                    "source": "NYC Open Data (311)"
                })
        elif "chicago" in place and state == "IL":
            url = "https://data.cityofchicago.org/resource/v6vf-nfxy.json"
            params = {"$limit": 10, "zip_code": zip}
            data = await fetch_json(url, params=params)
            for r in data:
                items.append({
                    "type": r.get("service_request_type") or "311",
                    "detail": r.get("description") or "Issue",
                    "status": r.get("status") or "",
                    "created": r.get("created_date") or "",
                    "source": "City of Chicago (311)"
                })
    except Exception:
        # soft-fail; show no updates if the open-data API is unavailable
        pass

    payload = {
        "count": len(items),
        "items": items,
        "note": "City updates supported for NYC & Chicago; more cities coming."
    }
    cache_set(key, payload, ttl=60 * 10)
    return payload

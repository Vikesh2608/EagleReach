from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx, asyncio, datetime, time, math, json
from typing import Optional, Dict, Any, Tuple

# -----------------------------------------------------------------------------
# App & CORS
# -----------------------------------------------------------------------------
app = FastAPI(title="EagleReach Backend (Open Data)")

ALLOWED = [
    "https://vikesh2608.github.io",   # GitHub Pages (your site)
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Open data sources
# -----------------------------------------------------------------------------
ZIPPOP = "https://api.zippopotam.us/us"
FCC    = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OVERPASS = "https://overpass-api.de/api/interpreter"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

UA = {"User-Agent": "EagleReach/1.2 (contact: vikebairam@gmail.com)"}

# -----------------------------------------------------------------------------
# Tiny TTL cache (in-memory)
# -----------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Any]] = {}

def cache_get(key: str):
    v = _cache.get(key)
    if not v:
        return None
    exp, data = v
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return data

def cache_set(key: str, data: Any, ttl: int = 300):
    _cache[key] = (time.time() + ttl, data)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _esc(s): 
    return (s or "").strip()

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def fallback_search_url(name:str, state_abbr:str, office:str):
    q = f"{name} {state_abbr} {office} official site"
    # minimal safe query param
    return "https://www.google.com/search?q=" + httpx.QueryParams({"q": q})["q"]

# -----------------------------------------------------------------------------
# Core lookups
# -----------------------------------------------------------------------------
async def fetch_zip(zip_code: str):
    """ZIP -> {state_abbr, state_name, place_name, lat, lon}"""
    zip_code = _esc(zip_code)
    url = f"{ZIPPOP}/{zip_code}"
    ck = f"zip:{zip_code}"
    c = cache_get(ck)
    if c: 
        return c
    async with httpx.AsyncClient(timeout=15, headers=UA) as client:
        r = await client.get(url)
        if r.status_code == 404:
            raise HTTPException(404, f"ZIP {zip_code} not found")
        r.raise_for_status()
        j = r.json()
        place = j["places"][0]
        out = {
            "state_abbr": _esc(place["state abbreviation"]),
            "state_name": _esc(place["state"]),
            "place_name": _esc(place["place name"]),
            "lat": float(place["latitude"]),
            "lon": float(place["longitude"]),
        }
        cache_set(ck, out, 3600)
        return out

async def fetch_cd(lat: float, lon: float):
    """lat/lon -> congressional district number (string or None)"""
    params = {"latitude": lat, "longitude": lon, "format": "json", "showall": "false"}
    ck = f"cd:{lat:.4f},{lon:.4f}"
    c = cache_get(ck)
    if c: 
        return c
    async with httpx.AsyncClient(timeout=15, headers=UA) as client:
        r = await client.get(FCC, params=params)
        r.raise_for_status()
        j = r.json()
        cd_obj = j.get("CongressionalDistrict")
        num: Optional[str] = None
        if isinstance(cd_obj, dict):
            num = _esc(cd_obj.get("code") or "").lstrip("0") or None
        out = {"district": num}
        cache_set(ck, out, 3600)
        return out

async def fetch_senators(state_abbr: str):
    """GovTrack: current senators for a state"""
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "senator", "state": state_abbr.upper()}
    ck = f"sen:{state_abbr}"
    c = cache_get(ck)
    if c: 
        return c
    rows = []
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        for it in j.get("objects", []):
            p = it.get("person", {}) or {}
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link")
            if not website:
                website = fallback_search_url(name, state_abbr, "United States Senator")
            rows.append({
                "name": name,
                "office": "United States Senator",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],  # GovTrack doesn’t expose emails
                "urls": [website] if website else [],
            })
    cache_set(ck, rows, 3600)
    return rows

async def fetch_rep(state_abbr: str, district: Optional[str]):
    """GovTrack: current representative for a state+district"""
    if not district:
        return []
    url = f"{GOVTRACK}/role"
    params = {
        "current": "true",
        "role_type": "representative",
        "state": state_abbr.upper(),
        "district": district,
    }
    ck = f"rep:{state_abbr}:{district}"
    c = cache_get(ck)
    if c: 
        return c
    rows = []
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        for it in j.get("objects", []):
            p = it.get("person", {}) or {}
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link")
            if not website:
                website = fallback_search_url(name, state_abbr, f"United States Representative CD {district}")
            rows.append({
                "name": name,
                "office": f"United States Representative (CD {district})",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],
                "urls": [website] if website else [],
            })
    cache_set(ck, rows, 3600)
    return rows

async def fetch_mayor(place_name: str, state_name: str):
    """
    Best-effort Mayor via Wikidata. If no website, add a Google 'official site' search.
    Returns 0..1 row in a list.
    """
    city = _esc(place_name)
    st = _esc(state_name)
    if not city or not st:
        return []
    q = f"""
    SELECT ?personLabel ?website WHERE {{
      ?city rdfs:label "{city}"@en ;
            wdt:P17 wd:Q30 ;
            wdt:P131* ?state ;
            wdt:P31/wdt:P279* wd:Q515 .
      ?state rdfs:label "{st}"@en .
      ?city wdt:P6 ?person .
      OPTIONAL {{ ?person wdt:P856 ?website. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 1
    """
    ck = f"mayor:{city},{st}"
    c = cache_get(ck)
    if c is not None:
        return c
    async with httpx.AsyncClient(timeout=20, headers={**UA, "Accept":"application/sparql-results+json"}) as client:
        r = await client.get(WIKIDATA_SPARQL, params={"query": q})
        if r.status_code != 200:
            cache_set(ck, [], 600)
            return []
        j = r.json()
        b = j.get("results", {}).get("bindings", []) or []
        if not b:
            cache_set(ck, [], 600)
            return []
        name = b[0].get("personLabel", {}).get("value")
        site = b[0].get("website", {}).get("value")
        url = site or fallback_search_url(name, st, "Mayor")
        if not name:
            cache_set(ck, [], 600)
            return []
        out = [{
            "name": name,
            "office": "Mayor",
            "party": None,
            "phones": [],
            "emails": [],
            "urls": [url] if url else [],
        }]
        cache_set(ck, out, 3600)
        return out

def normalize(rows):
    def order(row):
        off = (row.get("office") or "").lower()
        if "united states senator" in off: return 0
        if "united states representative" in off: return 1
        if "mayor" in off: return 2
        return 9
    return sorted(rows, key=order)

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "backend_version": "1.2.0-open-data",
        "sources": ["zippopotam.us", "FCC Census Block", "GovTrack", "Wikidata", "OpenStreetMap Overpass", "Open‑Meteo"],
    }

@app.get("/officials")
async def officials(zip: str = Query(..., min_length=5, max_length=10)):
    z = await fetch_zip(zip)
    state_abbr, state_name, place = z["state_abbr"], z["state_name"], z["place_name"]
    lat, lon = z["lat"], z["lon"]

    # parallel sub-queries
    cd_task     = asyncio.create_task(fetch_cd(lat, lon))
    sen_task    = asyncio.create_task(fetch_senators(state_abbr))
    mayor_task  = asyncio.create_task(fetch_mayor(place, state_name))

    cd = await cd_task
    district = cd.get("district")
    rep_rows  = await fetch_rep(state_abbr, district)
    sen_rows, mayor_rows = await asyncio.gather(sen_task, mayor_task)

    rows = normalize(sen_rows + rep_rows + mayor_rows)

    return {
        "zip": zip,
        "state": {"abbr": state_abbr, "name": state_name},
        "place": place,
        "location": {"lat": lat, "lon": lon},
        "district": district,
        "officials": rows,
        "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

# ----------------------- BMV / DMV (OpenStreetMap Overpass) -------------------
async def fetch_bmv(zip: str, radius_km: float = 25.0):
    """Query Overpass for DMV/BMV/vehicle-registration offices around ZIP center."""
    z = await fetch_zip(zip)
    lat, lon = z["lat"], z["lon"]

    q = f"""
    [out:json][timeout:25];
    (
      node(around:{int(radius_km*1000)},{lat},{lon})[office=government][government=vehicle_registration];
      way(around:{int(radius_km*1000)},{lat},{lon})[office=government][government=vehicle_registration];
      relation(around:{int(radius_km*1000)},{lat},{lon})[office=government][government=vehicle_registration];

      node(around:{int(radius_km*1000)},{lat},{lon})[name~"DMV|BMV|Department of Motor Vehicles|Bureau of Motor Vehicles", i];
      way(around:{int(radius_km*1000)},{lat},{lon})[name~"DMV|BMV|Department of Motor Vehicles|Bureau of Motor Vehicles", i];
      relation(around:{int(radius_km*1000)},{lat},{lon})[name~"DMV|BMV|Department of Motor Vehicles|Bureau of Motor Vehicles", i];
    );
    out center tags 30;
    """

    ck = f"bmv:{lat:.4f},{lon:.4f}:{radius_km}"
    c = cache_get(ck)
    if c:
        return c

    async with httpx.AsyncClient(timeout=30, headers={**UA, "Accept": "application/json"}) as client:
        r = await client.post(OVERPASS, data={"data": q})
        r.raise_for_status()
        j = r.json()

    out = []
    for el in j.get("elements", []):
        tags = el.get("tags", {}) or {}
        # coordinates
        if el.get("type") == "node":
            el_lat, el_lon = el.get("lat"), el.get("lon")
        else:
            c0 = el.get("center") or {}
            el_lat, el_lon = c0.get("lat"), c0.get("lon")
        if el_lat is None or el_lon is None:
            continue

        num   = tags.get("addr:housenumber", "")
        street= tags.get("addr:street", "")
        city  = tags.get("addr:city", "")
        state = tags.get("addr:state", "")
        pc    = tags.get("addr:postcode", "")
        address = " ".join([_esc(num), _esc(street)]).strip()
        locality = ", ".join([x for x in [_esc(city), _esc(state), _esc(pc)] if x])

        phone = tags.get("phone") or tags.get("contact:phone") or ""
        website = tags.get("website") or tags.get("contact:website") or ""
        hours = tags.get("opening_hours") or ""
        name = tags.get("name") or "DMV / BMV Office"
        dist = round(haversine_km(lat, lon, el_lat, el_lon), 1)

        out.append({
            "name": name,
            "address": address,
            "locality": locality,
            "phone": phone,
            "website": website,
            "opening_hours": hours,
            "lat": el_lat,
            "lon": el_lon,
            "distance_km": dist,
            "maps": f"https://www.google.com/maps/search/?api=1&query={el_lat},{el_lon}"
        })

    out.sort(key=lambda x: (x["distance_km"] if x.get("distance_km") is not None else 1e9))
    res = {
        "zip": zip,
        "location": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "offices": out[:50],
        "count": len(out)
    }
    cache_set(ck, res, 600)
    return res

@app.get("/bmv")
async def bmv_endpoint(zip: str, radius_km: float = 25.0):
    try:
        data = await fetch_bmv(zip=zip, radius_km=radius_km)
        if not isinstance(data, dict):
            data = {}
        offices = data.get("offices") or []
        count = data.get("count") or len(offices)
        return {
            "zip": zip,
            "location": data.get("location"),
            "radius_km": radius_km,
            "offices": offices,
            "count": count,
            "sources": ["OpenStreetMap Overpass"]
        }
    except Exception as e:
        print(f"/bmv error for zip={zip}: {e}")
        return {
            "zip": zip,
            "offices": [],
            "count": 0,
            "error": "BMV/DMV service temporarily unavailable",
            "fallback": f"https://www.google.com/maps/search/DMV+near+{zip}"
        }

# --------------------------- Weather (Open‑Meteo) -----------------------------
@app.get("/weather")
async def get_weather(zip: str):
    """
    7-day daily forecast (C) using Open‑Meteo.
    """
    try:
        z = await fetch_zip(zip)
        lat, lon = z["lat"], z["lon"]
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 7,
            "timezone": "auto",
        }
        async with httpx.AsyncClient(timeout=15, headers={"Accept":"application/json", **UA}) as client:
            r = await client.get(OPEN_METEO, params=params)
            r.raise_for_status()
            j = r.json()

        daily = j.get("daily", {}) or {}
        times = daily.get("time", []) or []
        tmax  = daily.get("temperature_2m_max", []) or []
        tmin  = daily.get("temperature_2m_min", []) or []
        ppop  = daily.get("precipitation_probability_max", []) or []

        out = []
        for i in range(min(len(times), len(tmax), len(tmin), len(ppop))):
            out.append({
                "date":       times[i],
                "tmax_c":     tmax[i],
                "tmin_c":     tmin[i],
                "precip_pct": ppop[i],
            })

        return {
            "zip": zip,
            "place": z.get("place_name"),
            "state": z.get("state_abbr"),
            "lat": lat,
            "lon": lon,
            "days": out,
            "updated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source": "Open‑Meteo",
        }
    except Exception as e:
        print(f"/weather error for zip={zip}: {e}")
        return {"zip": zip, "days": [], "error": "Weather temporarily unavailable"}

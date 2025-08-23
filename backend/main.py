from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx, asyncio, datetime, time, math
from typing import Optional, Dict, Any, Tuple

app = FastAPI(title="EagleReach Backend (Open Data)")

# --- CORS: allow GitHub Pages + localhost
ALLOWED = [
    "https://vikesh2608.github.io",
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

# --- Open-data sources (no keys)
ZIPPOP = "https://api.zippopotam.us/us"
FCC = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NYC311 = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
CHI311 = "https://data.cityofchicago.org/resource/v6vf-nfxy.json"

UA = {"User-Agent": "EagleReach/1.2 (contact: vikebairam@gmail.com)"}

def _esc(s): return (s or "").strip()

# --- tiny TTL cache
_cache: Dict[str, Tuple[float, Any]] = {}
def cache_get(k: str):
    v = _cache.get(k)
    if not v: return None
    exp, data = v
    if time.time() > exp:
        _cache.pop(k, None); return None
    return data
def cache_set(k: str, data: Any, ttl: int = 300):
    _cache[k] = (time.time() + ttl, data)

def haversine_km(lat1, lon1, lat2, lon2):
    R=6371.0
    p1=math.radians(lat1); p2=math.radians(lat2)
    dphi=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

@app.get("/health")
def health():
    return {
        "ok": True,
        "backend_version": "1.2.0-open-data",
        "sources": ["zippopotam.us", "FCC Census Block", "GovTrack", "Wikidata", "Open-Meteo", "NYC 311", "Chicago 311"]
    }

# --------------------------- ZIP + district helpers ---------------------------
async def fetch_zip(zip_code: str):
    """ZIP -> {state_abbr, state_name, place_name, lat, lon}"""
    ck = f"zip:{zip_code}"
    c = cache_get(ck)
    if c: return c
    url = f"{ZIPPOP}/{_esc(zip_code)}"
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
    params = {"latitude": lat, "longitude": lon, "format": "json", "showall": "false"}
    ck = f"cd:{lat:.4f},{lon:.4f}"
    c = cache_get(ck)
    if c: return c
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

def google_site(name: str, state_abbr: str, office: str):
    q = f"{name} {state_abbr} {office} official site"
    from urllib.parse import urlencode
    return f"https://www.google.com/search?{urlencode({'q': q})}"

# --------------------------- Congress + Mayor --------------------------------
async def fetch_senators(state_abbr: str):
    ck = f"sen:{state_abbr}"
    c = cache_get(ck)
    if c: return c
    params = {"current": "true", "role_type": "senator", "state": state_abbr.upper()}
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(f"{GOVTRACK}/role", params=params)
        r.raise_for_status()
        j = r.json()
    rows = []
    for it in j.get("objects", []):
        p = it.get("person", {}) or {}
        name = p.get("name") or p.get("name_long")
        website = it.get("website") or p.get("link") or google_site(name, state_abbr, "United States Senator")
        phones = [it.get("phone")] if it.get("phone") else []
        rows.append({"name": name, "office": "United States Senator", "party": it.get("party"),
                     "phones": phones, "emails": [], "urls": [website]})
    cache_set(ck, rows, 3600)
    return rows

async def fetch_rep(state_abbr: str, district: Optional[str]):
    if not district: return []
    ck = f"rep:{state_abbr}:{district}"
    c = cache_get(ck)
    if c: return c
    params = {"current": "true", "role_type": "representative", "state": state_abbr.upper(), "district": district}
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(f"{GOVTRACK}/role", params=params)
        r.raise_for_status()
        j = r.json()
    rows = []
    for it in j.get("objects", []):
        p = it.get("person", {}) or {}
        name = p.get("name") or p.get("name_long")
        website = it.get("website") or p.get("link") or google_site(name, state_abbr, f"United States Representative CD {district}")
        phones = [it.get("phone")] if it.get("phone") else []
        rows.append({"name": name, "office": f"United States Representative (CD {district})",
                     "party": it.get("party"), "phones": phones, "emails": [], "urls": [website]})
    cache_set(ck, rows, 3600)
    return rows

async def fetch_mayor(place_name: str, state_name: str):
    """Wikidata best‑effort mayor; fall back to a Google search link."""
    city = _esc(place_name); st = _esc(state_name)
    if not city or not st: return []
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
    if c is not None: return c
    async with httpx.AsyncClient(timeout=20, headers={**UA, "Accept": "application/sparql-results+json"}) as client:
        r = await client.get(WIKIDATA_SPARQL, params={"query": q})
        if r.status_code != 200:
            cache_set(ck, [], 600); return []
        j = r.json()
    b = (j.get("results", {}) or {}).get("bindings", [])
    if not b:
        cache_set(ck, [], 600); return []
    name = (b[0].get("personLabel") or {}).get("value")
    site = (b[0].get("website") or {}).get("value")
    if not name:
        cache_set(ck, [], 600); return []
    url = site or google_site(name, city, "Mayor")
    out = [{"name": name, "office": "Mayor", "party": None, "phones": [], "emails": [], "urls": [url]}]
    cache_set(ck, out, 3600)
    return out

def normalize(rows):
    def w(row):
        off = (row.get("office") or "").lower()
        if "united states senator" in off: return 0
        if "representative" in off: return 1
        if "mayor" in off: return 2
        return 9
    return sorted(rows, key=w)

@app.get("/officials")
async def officials(zip: str = Query(..., min_length=5, max_length=10)):
    z = await fetch_zip(zip)
    lat, lon = z["lat"], z["lon"]
    cd_task     = asyncio.create_task(fetch_cd(lat, lon))
    sen_task    = asyncio.create_task(fetch_senators(z["state_abbr"]))
    mayor_task  = asyncio.create_task(fetch_mayor(z["place_name"], z["state_name"]))
    cd = await cd_task
    rep_rows = await fetch_rep(z["state_abbr"], cd.get("district"))
    sen_rows, mayor_rows = await asyncio.gather(sen_task, mayor_task)
    rows = normalize(sen_rows + rep_rows + mayor_rows)
    return {
        "zip": zip,
        "state": {"abbr": z["state_abbr"], "name": z["state_name"]},
        "place": z["place_name"],
        "location": {"lat": lat, "lon": lon},
        "district": cd.get("district"),
        "officials": rows,
        "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "sources": {"zip": "zippopotam.us", "district": "FCC Census Block", "congress": "GovTrack", "mayor": "Wikidata"}
    }

# ----------------------------- Weather (Open‑Meteo) --------------------------
WEATHER_MAP = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    80: ("Rain showers", "🌦️"), 81: ("Rain showers", "🌦️"), 82: ("Heavy showers", "🌧️"),
    85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunder w/ hail", "⛈️"), 99: ("Thunder w/ hail", "⛈️"),
}

@app.get("/weather")
async def weather(zip: str):
    z = await fetch_zip(zip)
    lat, lon = z["lat"], z["lon"]
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 7, "timezone": "auto",
    }
    ck = f"wx:{lat:.4f},{lon:.4f}"
    c = cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=15, headers=UA) as client:
        r = await client.get(OPEN_METEO, params=params)
        r.raise_for_status()
        j = r.json()
    times = j.get("daily", {}).get("time", [])
    wcodes = j.get("daily", {}).get("weathercode", [])
    tmax = j.get("daily", {}).get("temperature_2m_max", [])
    tmin = j.get("daily", {}).get("temperature_2m_min", [])
    ppop = j.get("daily", {}).get("precipitation_probability_max", [])
    out = []
    for i in range(min(len(times), len(wcodes), len(tmax), len(tmin), len(ppop))):
        label, icon = WEATHER_MAP.get(int(wcodes[i]), ("", ""))
        out.append({
            "date": times[i],
            "label": label,
            "icon": icon,
            "tmax_c": tmax[i],
            "tmin_c": tmin[i],
            "precip_pct": ppop[i],
        })
    res = {
        "zip": zip, "place": z["place_name"], "state": z["state_abbr"],
        "lat": lat, "lon": lon, "days": out,
        "updated_at": datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "source": "Open‑Meteo"
    }
    cache_set(ck, res, 600)
    return res

# ----------------------------- Voter Info links ------------------------------
@app.get("/voter")
async def voter(zip: str):
    z = await fetch_zip(zip)
    st = z["state_abbr"].lower()
    return {
        "zip": zip, "state": z["state_abbr"],
        "register_url": f"https://www.vote.gov/register/{st}/",
        "howto_url": f"https://www.vote.gov/register/{st}/",
        "polling_url": "https://www.vote.org/polling-place-locator/",
        "education": "https://www.usa.gov/voting"
    }

# ----------------------------- City Updates (read) ---------------------------
@app.get("/city/updates")
async def city_updates(zip: str = Query(..., min_length=5, max_length=10), limit: int = 10):
    """Basic 311 feed for NYC/Chicago; returns top issues if zip matches city."""
    z = await fetch_zip(zip)
    place = (z["place_name"] or "").lower()
    out = {"zip": zip, "city": z["place_name"], "items": [], "source": None}
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            if "new york" in place or "ny" in place:
                params = {"$limit": limit, "$order": "created_date DESC", "incident_zip": zip}
                r = await client.get(NYC311, params=params)
                if r.status_code == 200:
                    arr = r.json()
                    items = []
                    for it in arr:
                        items.append({
                            "type": it.get("complaint_type"),
                            "detail": it.get("descriptor"),
                            "status": it.get("status"),
                            "address": it.get("incident_address"),
                            "created": it.get("created_date"),
                        })
                    out["items"] = items
                    out["source"] = "NYC Open Data (311)"
                    return out
            if "chicago" in place:
                params = {"$limit": limit, "$order": "creation_date DESC", "zip_code": zip}
                r = await client.get(CHI311, params=params)
                if r.status_code == 200:
                    arr = r.json()
                    items = []
                    for it in arr:
                        items.append({
                            "type": it.get("service_request_type"),
                            "detail": it.get("status"),
                            "status": it.get("status"),
                            "address": it.get("street_address"),
                            "created": it.get("creation_date"),
                        })
                    out["items"] = items
                    out["source"] = "Chicago Open Data (311)"
                    return out
    except Exception:
        pass
    out["items"] = []
    out["source"] = "No city feed for this ZIP"
    return out

# ----------------------------- News links helper -----------------------------
@app.get("/news")
async def news(zip: str):
    z = await fetch_zip(zip)
    from urllib.parse import urlencode
    local = "https://news.google.com/search?" + urlencode({"q": z["place_name"] or zip, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    world = "https://www.reuters.com/world/"
    return {"zip": zip, "local_url": local, "world_url": world}

# ----------------------------- Elections (light) -----------------------------
@app.get("/elections")
async def elections(zip: str):
    # Keeping this simple and clear; state‑specific primaries can be added later.
    return {
        "zip": zip,
        "next_federal": "November 3, 2025",
        "notes": "State primary dates vary by state; check your state’s official site from Voter Info."
    }

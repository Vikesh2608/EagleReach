from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any, Tuple, List
import httpx, asyncio, datetime, time, math

app = FastAPI(title="EagleReach Backend (Open Data)")

# ---- Allow GitHub Pages + localhost ----
ALLOWED = [
    "https://vikesh2608.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Open-data sources (no API keys) ----
ZIPPOP = "https://api.zippopotam.us/us"
FCC = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

UA = {"User-Agent": "EagleReach/2.0 (contact: vikebairam@gmail.com)"}

def _esc(s): return (s or "").strip()

# ---- tiny in-memory TTL cache ----
_cache: Dict[str, Tuple[float, Any]] = {}
def cache_get(key: str):
    v = _cache.get(key)
    if not v: return None
    exp, data = v
    if time.time() > exp:
        _cache.pop(key, None); return None
    return data
def cache_set(key: str, data: Any, ttl: int = 300):
    _cache[key] = (time.time() + ttl, data)

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
        "backend_version": "2.0-open-data",
        "sources": ["zippopotam.us", "FCC Census Block", "GovTrack", "Wikidata", "Open‑Meteo"],
    }

# ---------- ZIP/Place ----------
async def fetch_zip(zip_code:str):
    """ZIP -> {state_abbr, state_name, place_name, lat, lon}"""
    zip_code = _esc(zip_code)
    url = f"{ZIPPOP}/{zip_code}"
    ck = f"zip:{zip_code}"
    c = cache_get(ck)
    if c: return c
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

async def fetch_cd(lat:float, lon:float):
    """lat/lon -> congressional district number (string or None)"""
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

def fallback_search_url(name:str, state_abbr:str, office:str):
    import urllib.parse as up
    q = f"{name} {state_abbr} {office} official site"
    return f"https://www.google.com/search?q={up.quote(q)}"

# ---------- Congress & Mayor ----------
async def fetch_senators(state_abbr:str):
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "senator", "state": state_abbr.upper()}
    ck = f"sen:{state_abbr}"
    c = cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {}) or {}
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link") or fallback_search_url(name, state_abbr, "United States Senator")
            phone = it.get("phone")
            rows.append({
                "name": name,
                "office": "United States Senator",
                "party": it.get("party"),
                "phones": [phone] if phone else [],
                "emails": [],
                "urls": [website] if website else [],
            })
        cache_set(ck, rows, 3600)
        return rows

async def fetch_rep(state_abbr:str, district: Optional[str]):
    if not district: return []
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "representative", "state": state_abbr.upper(), "district": district}
    ck = f"rep:{state_abbr}:{district}"
    c = cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {}) or {}
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link") or fallback_search_url(name, state_abbr, f"United States Representative CD {district}")
            phone = it.get("phone")
            rows.append({
                "name": name,
                "office": f"United States Representative (CD {district})",
                "party": it.get("party"),
                "phones": [phone] if phone else [],
                "emails": [],
                "urls": [website] if website else [],
            })
        cache_set(ck, rows, 3600)
        return rows

async def fetch_mayor(place_name:str, state_name:str):
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
    async with httpx.AsyncClient(timeout=20, headers={**UA, "Accept":"application/sparql-results+json"}) as client:
        r = await client.get(WIKIDATA_SPARQL, params={"query": q})
        if r.status_code != 200:
            cache_set(ck, [], 600); return []
        j = r.json()
        b = j.get("results", {}).get("bindings", []) or []
        if not b:
            cache_set(ck, [], 600); return []
        name = b[0].get("personLabel", {}).get("value")
        site = b[0].get("website", {}).get("value")
        url = site or fallback_search_url(name, st, "Mayor")
        if not name:
            cache_set(ck, [], 600); return []
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
    def w(row):
        off = (row.get("office") or "").lower()
        if "united states senator" in off: return 0
        if "united states representative" in off: return 1
        if "mayor" in off: return 2
        return 9
    return sorted(rows, key=w)

@app.get("/officials")
async def officials(zip: str = Query(..., min_length=5, max_length=10)):
    z = await fetch_zip(zip)
    state_abbr, state_name, place = z["state_abbr"], z["state_name"], z["place_name"]
    lat, lon = z["lat"], z["lon"]

    cd_task     = asyncio.create_task(fetch_cd(lat, lon))
    senate_task = asyncio.create_task(fetch_senators(state_abbr))
    mayor_task  = asyncio.create_task(fetch_mayor(place, state_name))

    cd = await cd_task
    district = cd.get("district")
    rep_rows  = await fetch_rep(state_abbr, district)
    sen_rows, mayor_rows = await asyncio.gather(senate_task, mayor_task)

    rows = normalize(sen_rows + rep_rows + mayor_rows)

    return {
        "ok": True,
        "zip": zip,
        "state": {"abbr": state_abbr, "name": state_name},
        "place": place,
        "location": {"lat": lat, "lon": lon},
        "district": district,
        "officials": rows,
        "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sources": {
            "zip": "zippopotam.us",
            "district": "FCC Census Block",
            "congress": "GovTrack",
            "mayor": "Wikidata",
        }
    }

# ---------- Weather (Open‑Meteo) ----------
# Weather code mapping (Open‑Meteo doc)
W_CODE = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫️"), 48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Rain showers", "🌧️"), 81: ("Rain showers", "🌧️"), 82: ("Violent rain showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunder + hail", "⛈️"), 99: ("Thunder + hail", "⛈️"),
}
def code_to_text(code:int):
    return W_CODE.get(code, ("Weather", "🌤️"))

@app.get("/weather")
async def get_weather(zip: str = Query(..., min_length=5, max_length=10)):
    z = await fetch_zip(zip)
    lat, lon = z["lat"], z["lon"]
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 7,
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(OPEN_METEO, params=params)
        r.raise_for_status()
        j = r.json()
    daily = j.get("daily", {})
    times  = daily.get("time", [])
    maxs   = daily.get("temperature_2m_max", [])
    mins   = daily.get("temperature_2m_min", [])
    pops   = daily.get("precipitation_probability_max", [])
    codes  = daily.get("weathercode", [])

    days = []
    for i in range(min(len(times), len(maxs), len(mins), len(pops), len(codes))):
        text, icon = code_to_text(int(codes[i]))
        days.append({
            "date": times[i],
            "tmax_c": maxs[i],
            "tmin_c": mins[i],
            "precip_pct": pops[i],
            "code": codes[i],
            "summary": text,
            "icon": icon
        })
    return {
        "ok": True,
        "zip": zip,
        "place": z.get("place_name"),
        "state": z.get("state_abbr"),
        "lat": lat, "lon": lon,
        "days": days,
        "updated_at": datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "source": "Open‑Meteo"
    }

# ---------- Feedback (in-memory) ----------
FEEDBACK: List[Dict[str, Any]] = []

class FeedbackIn(BaseModel):
    name: str
    email: EmailStr
    message: str
    rating: int = 5

@app.get("/feedback")
def get_feedback(limit: int = 50):
    return {"items": FEEDBACK[-limit:]}

@app.post("/feedback")
def post_feedback(fd: FeedbackIn):
    rating = max(1, min(5, fd.rating or 5))
    item = {
        "name": fd.name.strip(),
        "email": fd.email,
        "message": fd.message.strip(),
        "rating": rating,
        "created_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    FEEDBACK.append(item)
    return {"ok": True}

# simple admin utility to clear test data
@app.delete("/feedback")
def clear_feedback(all: bool = False):
    if all:
        FEEDBACK.clear()
    return {"ok": True, "count": len(FEEDBACK)}

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, Tuple, List
import httpx, asyncio, time, math
from datetime import datetime, timedelta

app = FastAPI(title="EagleReach Backend (Open Data)")

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

# Open data endpoints
ZIPPOP           = "https://api.zippopotam.us/us"
FCC              = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK         = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL  = "https://query.wikidata.org/sparql"
OVERPASS         = "https://overpass-api.de/api/interpreter"
NYC_311          = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
CHI_311          = "https://data.cityofchicago.org/resource/v6vf-nfxy.json"
OPEN_METEO       = "https://api.open-meteo.com/v1/forecast"

UA = {"User-Agent": "EagleReach/1.3 (contact: vikebairam@gmail.com)"}

# --- simple TTL cache
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

def _esc(s): return (s or "").strip()

def haversine_km(lat1, lon1, lat2, lon2):
    R=6371.0
    p1=math.radians(lat1); p2=math.radians(lat2)
    dphi=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def fallback_search_url(name:str, state_abbr:str, office:str):
    q = f"{name} {state_abbr} {office} official site"
    return "https://www.google.com/search?q=" + httpx.QueryParams({"q": q})["q"]

@app.get("/health")
def health():
    return {
        "ok": True,
        "backend_version": "1.3.0",
        "sources": ["zippopotam.us", "FCC", "GovTrack", "Wikidata", "OpenStreetMap Overpass", "Open‑Meteo", "NYC 311", "Chicago 311"]
    }

# ---- ZIP -> lat/lon/place/state
async def fetch_zip(zip_code: str):
    zip_code = _esc(zip_code)
    ck = f"zip:{zip_code}"
    c = cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=15, headers=UA) as client:
        r = await client.get(f"{ZIPPOP}/{zip_code}")
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

# ---- Congressional District via FCC
async def fetch_cd(lat: float, lon: float):
    ck = f"cd:{lat:.4f},{lon:.4f}"
    c = cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=15, headers=UA) as client:
        r = await client.get(FCC, params={"latitude":lat,"longitude":lon,"format":"json","showall":"false"})
        r.raise_for_status()
        j = r.json()
        cd_obj = j.get("CongressionalDistrict")
        num = None
        if isinstance(cd_obj, dict):
            num = _esc(cd_obj.get("code") or "").lstrip("0") or None
        out = {"district": num}
        cache_set(ck, out, 3600)
        return out

# ---- Congress (GovTrack)
async def fetch_senators(state_abbr: str):
    ck = f"sen:{state_abbr}"
    c = cache_get(ck)
    if c: return c
    params = {"current":"true","role_type":"senator","state":state_abbr.upper()}
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(f"{GOVTRACK}/role", params=params)
        r.raise_for_status()
        j = r.json()
    rows=[]
    for it in j.get("objects", []):
        p = it.get("person", {}) or {}
        name = p.get("name") or p.get("name_long")
        website = it.get("website") or p.get("link") or fallback_search_url(name, state_abbr, "United States Senator")
        rows.append({
            "name": name,
            "office": "United States Senator",
            "party": it.get("party"),
            "phones": [it.get("phone")] if it.get("phone") else [],
            "emails": [],
            "urls": [website] if website else [],
        })
    cache_set(ck, rows, 3600)
    return rows

async def fetch_rep(state_abbr: str, district: Optional[str]):
    if not district: return []
    ck = f"rep:{state_abbr}:{district}"
    c = cache_get(ck)
    if c: return c
    params = {"current":"true","role_type":"representative","state":state_abbr.upper(),"district":district}
    async with httpx.AsyncClient(timeout=20, headers=UA) as client:
        r = await client.get(f"{GOVTRACK}/role", params=params)
        r.raise_for_status()
        j = r.json()
    rows=[]
    for it in j.get("objects", []):
        p = it.get("person", {}) or {}
        name = p.get("name") or p.get("name_long")
        website = it.get("website") or p.get("link") or fallback_search_url(name, state_abbr, f"United States Representative CD {district}")
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

# ---- Mayor via Wikidata (best effort)
async def fetch_mayor(place_name: str, state_name: str):
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
        "name": name, "office": "Mayor", "party": None,
        "phones": [], "emails": [], "urls": [url] if url else []
    }]
    cache_set(ck, out, 3600)
    return out

def normalize(rows: List[Dict[str, Any]]):
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
    sen_task    = asyncio.create_task(fetch_senators(state_abbr))
    mayor_task  = asyncio.create_task(fetch_mayor(place, state_name))
    cd = await cd_task
    district = cd.get("district")
    rep_rows = await fetch_rep(state_abbr, district)
    sen_rows, mayor_rows = await asyncio.gather(sen_task, mayor_task)
    rows = normalize(sen_rows + rep_rows + mayor_rows)
    return {
        "zip": zip,
        "state": {"abbr": state_abbr, "name": state_name},
        "place": place,
        "location": {"lat": lat, "lon": lon},
        "district": district,
        "officials": rows,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds")+"Z",
    }

# ---- Weather (Open‑Meteo)
@app.get("/weather")
async def weather(zip: str):
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
        out=[]
        for i in range(min(len(daily.get("time",[])), len(daily.get("temperature_2m_max",[])), len(daily.get("temperature_2m_min",[])))):
            out.append({
                "date": daily["time"][i],
                "tmax_c": daily["temperature_2m_max"][i],
                "tmin_c": daily["temperature_2m_min"][i],
                "precip_pct": (daily.get("precipitation_probability_max") or [None]*99)[i]
            })
        return {
            "zip": zip,
            "place": z.get("place_name"), "state": z.get("state_abbr"),
            "lat": lat, "lon": lon, "days": out,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds")+"Z",
            "source": "Open‑Meteo"
        }
    except Exception as e:
        print(f"/weather error {zip}: {e}")
        return {"zip": zip, "days": [], "error": "Weather temporarily unavailable"}

# ---- Roadworks (traffic signal) via OSM
@app.get("/roadworks")
async def roadworks(zip: str, radius_km: float = 10.0):
    z = await fetch_zip(zip)
    lat, lon = z["lat"], z["lon"]
    q = f"""
    [out:json][timeout:25];
    (
      node(around:{int(radius_km*1000)},{lat},{lon})[highway=construction];
      way(around:{int(radius_km*1000)},{lat},{lon})[highway=construction];
      relation(around:{int(radius_km*1000)},{lat},{lon})[highway=construction];
      way(around:{int(radius_km*1000)},{lat},{lon})[construction];
      relation(around:{int(radius_km*1000)},{lat},{lon})[construction];
    );
    out center tags 50;
    """
    async with httpx.AsyncClient(timeout=30, headers={**UA, "Accept":"application/json"}) as client:
        r = await client.post(OVERPASS, data={"data": q})
        r.raise_for_status()
        j = r.json()
    items=[]
    for el in j.get("elements", []):
        tags = el.get("tags", {}) or {}
        n = tags.get("name") or tags.get("road") or "Road works"
        typ = tags.get("construction") or tags.get("highway")
        when = tags.get("opening_date") or tags.get("start_date") or None
        center = el.get("center") or {}
        el_lat = el.get("lat") or center.get("lat")
        el_lon = el.get("lon") or center.get("lon")
        dist = None
        if el_lat is not None and el_lon is not None:
            dist = round(haversine_km(lat, lon, el_lat, el_lon), 1)
        items.append({"name": n, "kind": typ, "when": when, "lat": el_lat, "lon": el_lon, "distance_km": dist})
    items.sort(key=lambda x: (x["distance_km"] if x["distance_km"] is not None else 9e9))
    return {"zip": zip, "location":{"lat":lat,"lon":lon}, "radius_km": radius_km, "items": items[:30], "count": len(items), "source":"OpenStreetMap Overpass"}

# ---- City Updates (NYC & Chicago 311)
def _since_iso(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

@app.get("/city/updates")
async def city_updates(zip: str, radius_km: float = 5.0, days: int = 14):
    z = await fetch_zip(zip)
    place = (z["place_name"] or "").lower()
    state = (z["state_abbr"] or "").upper()
    lat, lon = z["lat"], z["lon"]
    meters = int(max(0.5, radius_km) * 1000)
    since  = _since_iso(days)

    if state == "NY" and ("new york" in place or place in {"manhattan","brooklyn","queens","bronx","staten island"}):
        params = {"$select":"complaint_type,descriptor,status,incident_address,created_date",
                  "$where": f"within_circle(location,{lat},{lon},{meters}) AND created_date > '{since}'",
                  "$order":"created_date DESC", "$limit":"25"}
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.get(NYC_311, params=params)
            r.raise_for_status()
            rows = r.json()
        items = [{"type":i.get("complaint_type"),"detail":i.get("descriptor"),"status":i.get("status"),
                  "address":i.get("incident_address"),"created":i.get("created_date")} for i in rows]
        return {"zip":zip,"city":"New York City, NY","location":{"lat":lat,"lon":lon},"since":since,"radius_km":radius_km,
                "items":items,"count":len(items),"source":"NYC Open Data (311)"}

    if state == "IL" and "chicago" in place:
        params = {"$select":"service_name,status,street_address,creation_date",
                  "$where": f"within_circle(location,{lat},{lon},{meters}) AND creation_date > '{since}'",
                  "$order":"creation_date DESC", "$limit":"25"}
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.get(CHI_311, params=params)
            r.raise_for_status()
            rows = r.json()
        items = [{"type":i.get("service_name"),"status":i.get("status"),
                  "address":i.get("street_address"),"created":i.get("creation_date")} for i in rows]
        return {"zip":zip,"city":"Chicago, IL","location":{"lat":lat,"lon":lon},"since":since,"radius_km":radius_km,
                "items":items,"count":len(items),"source":"Chicago Open Data (311)"}

    return {"zip": zip, "location":{"lat":lat,"lon":lon}, "items": [], "count": 0,
            "note": "City updates currently available for NYC & Chicago; more coming soon."}

# ---- Voter info (official links by state)
STATE_OFFICE_SEARCH = "https://www.google.com/search?q={state}+election+office+site:.gov"
VOTE_GOV = "https://www.vote.gov/"
NASS = "https://www.nass.org/can-I-vote"
EAC_DIR = "https://www.eac.gov/voters/election-day-contact-information"

@app.get("/voter")
async def voter(zip: str):
    z = await fetch_zip(zip)
    state = z["state_name"]; abbr = z["state_abbr"]
    # Official links (general, per-state via search)
    return {
        "zip": zip,
        "state": {"name": state, "abbr": abbr},
        "links": {
            "register": VOTE_GOV,
            "state_office": STATE_OFFICE_SEARCH.format(state=state.replace(" ","+")),
            "how_to_vote": NASS,
            "county_directory": EAC_DIR
        },
        "note": "Links point to official state/county election resources."
    }

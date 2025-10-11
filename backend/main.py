# backend/main.py
import os
import re
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
import feedparser
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

API_TIMEOUT = float(os.getenv("API_TIMEOUT", "10.0"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24h

DEFAULT_ORIGINS = ",".join([
    "https://vikesh2608.github.io",
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:3000", "http://127.0.0.1:3000",
])
ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o]

ZIP_RE = re.compile(r"^\d{5}$")
STATE_ABBR = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())

# Free sources
ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"
FCC_AREAS_URL = "https://geo.fcc.gov/api/census/area?lat={lat}&lon={lon}&format=json"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
LEGIS_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.yaml"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

def google_news_search_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

WORLD_NEWS_RSS = "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"
US_NEWS_RSS    = "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en"

app = FastAPI(title="EagleReach (Free stack)", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: Dict[str, Tuple[float, Any]] = {}
def cache_get(key: str):
    v = _cache.get(key)
    if not v: return None
    ts, data = v
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None); return None
    return data
def cache_set(key: str, data: Any): _cache[key] = (time.time(), data)

client: Optional[httpx.AsyncClient] = None
@app.on_event("startup")
async def _startup():
    global client
    client = httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers={"User-Agent": "EagleReach/3.0 (+https://github.com/Vikesh2608/EagleReach)"},
        follow_redirects=True,
    )
@app.on_event("shutdown")
async def _shutdown():
    global client
    if client: await client.aclose(); client = None

async def fetch_json(url: str, *, params=None, headers=None, cache_key: Optional[str]=None, retries: int=2) -> Any:
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None: return cached
    if client is None: raise HTTPException(500, "HTTP client not ready")
    err: Optional[Exception] = None
    for i in range(retries+1):
        try:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code in (429,500,502,503,504) and i < retries:
                await asyncio.sleep(0.6*(i+1)); continue
            r.raise_for_status()
            data = r.json()
            if cache_key: cache_set(cache_key, data)
            return data
        except Exception as e:
            err = e
            if i < retries: await asyncio.sleep(0.6*(i+1))
    raise HTTPException(502, f"Upstream error for {url}: {err}")

async def fetch_text(url: str, *, params=None, headers=None, cache_key: Optional[str]=None, retries: int=1) -> str:
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None: return cached
    if client is None: raise HTTPException(500, "HTTP client not ready")
    err: Optional[Exception] = None
    for i in range(retries+1):
        try:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code in (429,500,502,503,504) and i < retries:
                await asyncio.sleep(0.6*(i+1)); continue
            r.raise_for_status()
            if cache_key: cache_set(cache_key, r.text)
            return r.text
        except Exception as e:
            err = e
            if i < retries: await asyncio.sleep(0.6*(i+1))
    raise HTTPException(502, f"Upstream text error for {url}: {err}")

async def zippopotam_info(zipcode: str) -> Dict[str, Any]:
    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode), cache_key=f"zip:{zipcode}")
    places = data.get("places") or []
    if not places: raise HTTPException(404, "ZIP not found")
    p = places[0]
    return {
        "zip": zipcode,
        "city": p.get("place name"),
        "state": p.get("state abbreviation"),
        "state_full": p.get("state"),
        "lat": float(p.get("latitude")),
        "lon": float(p.get("longitude")),
    }

async def nominatim_reverse(lat: float, lon: float) -> Dict[str, Any]:
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1}
    headers = {"User-Agent": "EagleReach/3.0 contact: example@example.com"}
    return await fetch_json(NOMINATIM_REVERSE, params=params, headers=headers, cache_key=f"rev:{round(lat,4)}:{round(lon,4)}")

async def fcc_district(lat: float, lon: float) -> Dict[str, Any]:
    data = await fetch_json(FCC_AREAS_URL.format(lat=lat, lon=lon), cache_key=f"fcc:{round(lat,4)}:{round(lon,4)}")
    res = (data.get("results") or [{}])[0]
    return {
        "state": res.get("state_code"),
        "district": str(res.get("Congressional District") or "").zfill(2) if res.get("Congressional District") else None,
        "county_name": res.get("county_name"),
    }

async def load_legislators() -> List[Dict[str, Any]]:
    text = await fetch_text(LEGIS_URL, cache_key="legis_yaml", retries=2)
    return yaml.safe_load(text)

def congress_people(data: List[Dict[str, Any]], state: str, district: Optional[str]) -> Dict[str, Any]:
    senators: List[Dict[str, Any]] = []
    rep: Optional[Dict[str, Any]] = None
    for person in data:
        terms = person.get("terms") or []
        if not terms: continue
        t = terms[-1]
        if t.get("type") == "sen" and t.get("state") == state:
            senators.append({
                "name": person["name"].get("official_full") or person["name"].get("last"),
                "party": t.get("party"),
                "website": t.get("url"),
                "phone": t.get("phone"),
                "twitter": (person.get("ids") or {}).get("twitter"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{person['id']['bioguide']}.jpg"
            })
        if district and t.get("type") == "rep" and t.get("state") == state and str(t.get("district")) == str(int(district)):
            rep = {
                "name": person["name"].get("official_full") or person["name"].get("last"),
                "party": t.get("party"),
                "website": t.get("url"),
                "phone": t.get("phone"),
                "twitter": (person.get("ids") or {}).get("twitter"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{person['id']['bioguide']}.jpg"
            }
    return {"senators": senators, "representative": rep}

async def wikidata_mayor(city: str, state_full: str) -> Optional[Dict[str, Any]]:
    if not city or not state_full: return None
    query = f"""
    SELECT ?mayor ?mayorLabel ?website WHERE {{
      ?city rdfs:label "{city}"@en ;
            wdt:P17 wd:Q30 ;
            wdt:P131 ?state .
      ?state rdfs:label "{state_full}"@en .
      OPTIONAL {{ ?city wdt:P6 ?mayor . }}
      OPTIONAL {{ ?mayor wdt:P856 ?website . }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 1
    """
    try:
        r = await client.get(WIKIDATA_SPARQL, params={"format":"json","query":query},
                             headers={"Accept":"application/sparql-results+json"})
        r.raise_for_status()
        data = r.json()
        rows = data.get("results", {}).get("bindings", [])
        if not rows: return None
        name = rows[0].get("mayorLabel", {}).get("value")
        website = rows[0].get("website", {}).get("value")
        return {"name": name, "website": website} if name else None
    except Exception:
        return None

def next_federal_general_election() -> str:
    import datetime as dt
    today = dt.date.today()
    year = today.year if today.year % 2 == 0 else today.year + 1
    d = dt.date(year, 11, 1)
    first_monday = d + dt.timedelta(days=(7 - d.weekday()) % 7)
    election = first_monday + dt.timedelta(days=1)
    return election.strftime("%Y-%m-%d")

def state_election_office_url(state_full: str) -> str:
    return "https://www.eac.gov/voters/register-and-vote-in-your-state"

def vote_registration_url(state_full: str) -> str:
    return "https://www.vote.gov/"

def parse_rss(url: str, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:limit]:
            out.append({
                "title": e.get("title"),
                "link": e.get("link"),
                "published": e.get("published"),
            })
        return out
    except Exception:
        return []

@app.get("/")
def home(): return {"ok": True, "service": "EagleReach (Free stack)"}
@app.get("/health")
def health(): return {"ok": True, "ts": int(time.time())}

@app.get("/revgeo")
async def revgeo(lat: float, lon: float):
    data = await nominatim_reverse(lat, lon)
    addr = data.get("address") or {}
    zipc = (addr.get("postcode") or "").split("-")[0]
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")
    state = addr.get("state")
    state_code = addr.get("state_code") or addr.get("ISO3166-2-lvl4", "").split("-")[-1]
    out = {"zip": zipc or None, "city": city, "state": state_code or None, "state_full": state}
    try:
        f = await fcc_district(lat, lon)
        out["congressional_district"] = f.get("district")
        if not out.get("state") and f.get("state"): out["state"] = f["state"]
    except Exception:
        pass
    return out

@app.get("/officials")
async def officials(zip: str = Query(..., description="US 5-digit ZIP code")):
    if not ZIP_RE.match(zip): raise HTTPException(400, "Invalid ZIP. Use 5 digits.")
    loc = await zippopotam_info(zip)
    fcc = await fcc_district(loc["lat"], loc["lon"])
    state = fcc.get("state") or loc["state"]
    district = fcc.get("district")
    if not state or state not in STATE_ABBR:
        raise HTTPException(502, "State unknown from FCC; try a different ZIP.")
    data = await load_legislators()
    fed = congress_people(data, state, district)
    mayor = await wikidata_mayor(loc["city"], loc["state_full"])
    return {
        "location": loc | {"district": district},
        "officials": {"senators": fed["senators"], "representative": fed["representative"], "mayor": mayor},
        "sources": {
            "zip": "Zippopotam.us",
            "district": "FCC Census Block API",
            "federal": "congress-legislators (GitHub)",
            "mayor": "Wikidata (best-effort)"
        }
    }

@app.get("/elections")
async def elections(state: str = Query(..., description="State USPS code, e.g., OH")):
    state = state.upper()
    if state not in STATE_ABBR: raise HTTPException(400, "Provide a valid 2-letter state code.")
    return {
        "next_federal_general": next_federal_general_election(),
        "state_resources": {
            "register_to_vote": vote_registration_url(state),
            "state_election_office": state_election_office_url(state),
        }
    }

@app.get("/news")
async def news(city: Optional[str] = None, state: Optional[str] = None,
               scope: str = Query("local", pattern="^(local|national|international)$")):
    if scope == "local":
        q = " ".join([x for x in [city, state] if x]).strip()
        if not q: raise HTTPException(400, "Provide city and state for local news.")
        url = google_news_search_rss(httpx.QueryParams({"q": q}).get("q"))
        items = parse_rss(url, limit=10)
    elif scope == "national":
        items = parse_rss(US_NEWS_RSS, limit=10)
    else:
        items = parse_rss(WORLD_NEWS_RSS, limit=10)
    return {"scope": scope, "items": items}

@app.get("/register")
async def register(state: str = Query(..., description="State USPS code, e.g., OH")):
    state = state.upper()
    if state not in STATE_ABBR: raise HTTPException(400, "Provide a valid 2-letter state code.")
    return {"learn": "https://www.vote.gov/", "state_election_office": state_election_office_url(state)}

@app.get("/emergency")
async def emergency():
    return {
        "emergency": [
            {"title": "Emergency (Police/Fire/EMS)", "number": "911"},
            {"title": "988 Suicide & Crisis Lifeline", "number": "988"},
            {"title": "211 Community Services", "number": "211"},
            {"title": "Non-emergency City Services (varies)", "number": "311 (where available)"},
        ],
        "notes": "For potholes, street lights, trash, etc., check your city's 311 portal."
    }

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("UNCAUGHT:", repr(exc))
    return JSONResponse(status_code=500, content={"error": "internal_server_error", "detail": str(exc)})

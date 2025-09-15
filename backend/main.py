import os
import re
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# -----------------------------
# Config
# -----------------------------
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "8.0"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 minutes

DEFAULT_ORIGINS = ",".join([
    # Put your GitHub Pages + local dev origins here
    "https://vikesh2608.github.io",
    "https://vikesh2608.github.io/EagleReach",
    "https://*.github.dev",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000"
])
ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o]

ZIP_RE = re.compile(r"^\d{5}$")

ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"
WMR_HOUSE_URL = "https://whoismyrepresentative.com/getall_mems.php?zip={zip}&output=json"
GOVTRACK_SENATE_URL = "https://www.govtrack.us/api/v2/role?current=true&role=senator&state={state}"
GOVTRACK_HOUSE_URL = "https://www.govtrack.us/api/v2/role?current=true&role=representative&state={state}&district={district}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

app = FastAPI(title="EagleReach API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Simple in-memory TTL cache
# -----------------------------
_cache: Dict[str, Tuple[float, Any]] = {}

def cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value

def cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)

# -----------------------------
# HTTP client (created on startup)
# -----------------------------
client: Optional[httpx.AsyncClient] = None

@app.on_event("startup")
async def on_startup():
    global client
    client = httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers={"User-Agent": "EagleReach/1.0 (https://github.com/Vikesh2608)"},
        follow_redirects=True,
    )

@app.on_event("shutdown")
async def on_shutdown():
    global client
    if client:
        await client.aclose()
        client = None

# -----------------------------
# Helpers
# -----------------------------
def clean_party(p: Optional[str]) -> Optional[str]:
    if not p:
        return p
    p = p.strip()
    mapping = {
        "Democrat": "Democratic",
        "Democratic": "Democratic",
        "Republican": "Republican",
        "Independent": "Independent",
    }
    return mapping.get(p, p)

def govtrack_role_to_person(role: Dict[str, Any]) -> Dict[str, Any]:
    person = role.get("person") or {}
    pid = person.get("id")
    website = (
        role.get("website")
        or role.get("extra", {}).get("url")
        or person.get("link")
    )
    photo = f"https://www.govtrack.us/static/legisphotos/{pid}-200px.jpeg" if pid else None
    extras = role.get("extras") or role.get("extra") or {}
    twitter = extras.get("twitter") or extras.get("twitter_id") if isinstance(extras, dict) else None
    return {
        "name": person.get("name"),
        "party": clean_party(role.get("party")),
        "state": role.get("state"),
        "district": role.get("district"),
        "role": role.get("role_type_label") or role.get("role_type"),
        "website": website,
        "phone": role.get("phone"),
        "contact_form": role.get("contact_form"),
        "twitter": twitter,
        "photo": photo,
        "govtrack_id": pid,
    }

async def fetch_json(url: str, cache_key: Optional[str] = None) -> Any:
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached
    if client is None:
        raise HTTPException(status_code=500, detail="HTTP client not ready.")
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    if cache_key:
        cache_set(cache_key, data)
    return data

async def zippopotam_info(zipcode: str) -> Dict[str, Any]:
    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode), f"zip:{zipcode}")
    places = data.get("places") or []
    if not places:
        raise HTTPException(status_code=404, detail="ZIP not found.")
    p = places[0]
    return {
        "zip": zipcode,
        "city": p.get("place name"),
        "state": p.get("state abbreviation"),
        "state_full": p.get("state"),
        "lat": float(p.get("latitude")),
        "lon": float(p.get("longitude")),
    }

async def whois_house(zipcode: str) -> Optional[Dict[str, Any]]:
    """Get a quick House district from WhoIsMyRepresentative (can be flaky)."""
    try:
        data = await fetch_json(WMR_HOUSE_URL.format(zip=zipcode), f"wmr:{zipcode}")
    except Exception:
        return None
    results = data.get("results") or []
    for r in results:
        d = (r.get("district") or "").strip()
        if d.isdigit():
            return {"district": d}
    return None

async def govtrack_senators(state: str) -> List[Dict[str, Any]]:
    data = await fetch_json(GOVTRACK_SENATE_URL.format(state=state), f"sen:{state}")
    return [govtrack_role_to_person(o) for o in data.get("objects", [])]

async def govtrack_representatives(state: str, district: str) -> List[Dict[str, Any]]:
    data = await fetch_json(GOVTRACK_HOUSE_URL.format(state=state, district=district), f"rep:{state}:{district}")
    return [govtrack_role_to_person(o) for o in data.get("objects", [])]

async def wikidata_mayor(city: str, state_full: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort lookup of head of government (P6) for a U.S. city.
    """
    if client is None:
        return None
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
        r = await client.get(
            WIKIDATA_SPARQL,
            params={"format": "json", "query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("results", {}).get("bindings", [])
        if not rows:
            return None
        row = rows[0]
        name = row.get("mayorLabel", {}).get("value")
        website = row.get("website", {}).get("value")
        if not name:
            return None
        return {"name": name, "website": website}
    except Exception:
        return None

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}

@app.get("/officials")
async def officials(zip: str = Query(..., description="US 5-digit ZIP code")):
    if not ZIP_RE.match(zip):
        raise HTTPException(status_code=400, detail="Invalid ZIP. Use 5 digits.")
    loc = await zippopotam_info(zip)
    state = loc["state"]
    city = loc["city"]
    state_full = loc["state_full"]

    # Parallel upstream lookups
    wmr_task = whois_house(zip)
    sen_task = govtrack_senators(state)
    mayor_task = wikidata_mayor(city, state_full)

    wmr, senators, mayor = await asyncio.gather(wmr_task, sen_task, mayor_task)

    representatives: List[Dict[str, Any]] = []
    if wmr and wmr.get("district"):
        try:
            representatives = await govtrack_representatives(state, wmr["district"])
        except Exception:
            representatives = []

    payload = {
        "location": {
            "zip": loc["zip"],
            "city": loc["city"],
            "state": state,
            "state_full": state_full,
            "lat": loc["lat"],
            "lon": loc["lon"],
        },
        "officials": {
            "senators": senators,
            "representatives": representatives,
            "mayor": mayor,  # may be None
        },
        "sources": {
            "zip": "Zippopotam.us",
            "district": "WhoIsMyRepresentative (ZIP → district)",
            "congress": "GovTrack.us",
            "mayor": "Wikidata",
        },
    }

    if not senators and not representatives and not mayor:
        # All upstream providers failed — surface a helpful error
        raise HTTPException(status_code=502, detail="Civic data providers unavailable. Try again shortly.")
    return payload

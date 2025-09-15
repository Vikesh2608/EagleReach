import os
import re
import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# =========================
# Config
# =========================
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "8.0"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))

DEFAULT_ORIGINS = ",".join([
    "https://vikesh2608.github.io",
    "https://vikesh2608.github.io/EagleReach",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
])
ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o]

ZIP_RE = re.compile(r"^\d{5}$")
ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"
WMR_HOUSE_URL = "https://whoismyrepresentative.com/getall_mems.php?zip={zip}&output=json"
GOVTRACK_SENATE_URL = "https://www.govtrack.us/api/v2/role?current=true&role=senator&state={state}"
GOVTRACK_HOUSE_URL = "https://www.govtrack.us/api/v2/role?current=true&role=representative&state={state}&district={district}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

app = FastAPI(title="EagleReach API", version="1.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Simple TTL cache
# =========================
_cache: Dict[str, Tuple[float, Any]] = {}

def cache_get(key: str):
    v = _cache.get(key)
    if not v:
        return None
    ts, data = v
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return data

def cache_set(key: str, data: Any):
    _cache[key] = (time.time(), data)

# =========================
# HTTP client
# =========================
client: Optional[httpx.AsyncClient] = None

@app.on_event("startup")
async def on_startup():
    global client
    client = httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers={"User-Agent": "EagleReach/1.0"},
        follow_redirects=True,
    )

@app.on_event("shutdown")
async def on_shutdown():
    global client
    if client:
        await client.aclose()
        client = None

# =========================
# Helpers
# =========================
def clean_party(p: Optional[str]) -> Optional[str]:
    if not p:
        return p
    p = p.strip()
    mapping = {"Democrat": "Democratic", "Democratic": "Democratic", "Republican": "Republican", "Independent": "Independent"}
    return mapping.get(p, p)

def gt_role_to_person(role: Dict[str, Any]) -> Dict[str, Any]:
    person = role.get("person") or {}
    pid = person.get("id")
    website = role.get("website") or role.get("extra", {}).get("url") or person.get("link")
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
    """GET + JSON with caching and clear 502 on upstream errors."""
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached
    if client is None:
        raise HTTPException(status_code=500, detail="HTTP client not ready")
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        # convert upstream 4xx/5xx to our 502 with details
        raise HTTPException(status_code=502, detail=f"Upstream {e.response.status_code} for {url}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error for {url}") from e
    if cache_key:
        cache_set(cache_key, data)
    return data

async def zippopotam_info(zipcode: String := str) -> Dict[str, Any]:
    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode), f"zip:{zipcode}")
    places = data.get("places") or []
    if not places:
        raise HTTPException(status_code=404, detail="ZIP not found")
    p = places[0]
    return {
        "zip": zipcode,
        "city": p.get("place name"),
        "state": p.get("state abbreviation"),
        "state_full": p.get("state"),
        "lat": float(p.get("latitude")),
        "lon": float(p.get("longitude")),
    }

async def whois_house(zipcode: str) -> Optional[str]:
    # This endpoint is flaky and sometimes returns HTML; be defensive.
    try:
        data = await fetch_json(WMR_HOUSE_URL.format(zip=zipcode), f"wmr:{zipcode}")
        results = data.get("results") or []
        for r in results:
            d = (r.get("district") or "").strip()
            if d.isdigit():
                return d
    except Exception:
        return None
    return None

async def govtrack_senators(state: str) -> List[Dict[str, Any]]:
    data = await fetch_json(GOVTRACK_SENATE_URL.format(state=state), f"sen:{state}")
    return [gt_role_to_person(o) for o in data.get("objects", [])]

async def govtrack_representatives(state: str, district: str) -> List[Dict[str, Any]]:
    data = await fetch_json(GOVTRACK_HOUSE_URL.format(state=state, district=district), f"rep:{state}:{district}")
    return [gt_role_to_person(o) for o in data.get("objects", [])]

async def wikidata_mayor(city: str, state_full: str) -> Optional[Dict[str, Any]]:
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

# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"ok": True, "service": "EagleReach API"}

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}

@app.get("/officials")
async def officials(zip: str = Query(..., description="US 5-digit ZIP code")):
    if not ZIP_RE.match(zip):
        raise HTTPException(status_code=400, detail="Invalid ZIP. Use 5 digits.")

    # location first; if that fails we want a clear error
    loc = await zippopotam_info(zip)
    state = loc["state"]
    city = loc["city"]
    state_full = loc["state_full"]

    # parallel calls
    wmr_task = whois_house(zip)
    sen_task = govtrack_senators(state)
    mayor_task = wikidata_mayor(city, state_full)
    district, senators, mayor = await asyncio.gather(wmr_task, sen_task, mayor_task)

    reps: List[Dict[str, Any]] = []
    if district:
        try:
            reps = await govtrack_representatives(state, district)
        except Exception:
            reps = []

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
            "representatives": reps,
            "mayor": mayor,
        },
        "sources": {
            "zip": "Zippopotam.us",
            "district": "WhoIsMyRepresentative (best-effort)",
            "congress": "GovTrack.us",
            "mayor": "Wikidata",
        },
    }

    if not senators and not reps and not mayor:
        # All upstreams failed but we *did* resolve the location; surface useful message.
        raise HTTPException(status_code=502, detail="Civic data providers unavailable. Try again later.")
    return payload

# =========================
# Global error handler -> JSON
# =========================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Log to stdout (visible in Render logs)
    print("UNCAUGHT ERROR:", repr(exc))
    return JSONResponse(status_code=500, content={"error": "internal_server_error", "detail": str(exc)})

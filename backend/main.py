# backend/main.py
import os
import re
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# =========================
# Config
# =========================
API_TIMEOUT = float(os.getenv("API_TIMEOUT", "10.0"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 min

GCIVIC_API_KEY = os.getenv("GCIVIC_API_KEY", "").strip()  # REQUIRED
OPENSTATES_API_KEY = os.getenv("OPENSTATES_API_KEY", "").strip()  # optional

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
GCIVIC_REPS_URL = "https://civicinfo.googleapis.com/civicinfo/v2/representatives"
OPENSTATES_PEOPLE_URL = "https://v3.openstates.org/people"  # v3 REST

app = FastAPI(title="EagleReach (Civic v2)", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Simple in-memory TTL cache
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
        headers={"User-Agent": "EagleReach/2.0 (+https://github.com/Vikesh2608/EagleReach)"},
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
async def fetch_json(url: str, *, params: Dict[str, Any] = None, headers: Dict[str, str] = None,
                     cache_key: Optional[str] = None, retries: int = 2) -> Any:
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    if client is None:
        raise HTTPException(status_code=500, detail="HTTP client not ready")

    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504) and i < retries:
                await asyncio.sleep(0.6 * (i + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if cache_key:
                cache_set(cache_key, data)
            return data
        except Exception as e:
            last_err = e
            if i < retries:
                await asyncio.sleep(0.6 * (i + 1))
            else:
                raise HTTPException(status_code=502, detail=f"Upstream error for {url}") from e
    raise HTTPException(status_code=502, detail=str(last_err) if last_err else "Upstream error")

async def zippopotam_info(zipcode: str) -> Dict[str, Any]:
    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode), cache_key=f"zip:{zipcode}")
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

def gcivic_office_map(offices: List[Dict[str, Any]], officials: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten Google Civic 'offices' + 'officials' arrays into structured roles."""
    results = []
    for off in offices or []:
        name = off.get("name")
        division_id = off.get("divisionId")
        levels = off.get("levels") or []
        roles = off.get("roles") or []
        indices = off.get("officialIndices") or []

        for idx in indices:
            if idx is None or idx >= len(officials):
                continue
            o = officials[idx] or {}
            phones = o.get("phones") or []
            urls = o.get("urls") or []
            photo = o.get("photoUrl")
            party = o.get("party")
            channels = {c["type"].lower(): c["id"] for c in (o.get("channels") or []) if "type" in c and "id" in c}
            twitter = channels.get("twitter")
            fb = channels.get("facebook")

            results.append({
                "office": name,
                "division_id": division_id,
                "levels": levels,
                "roles": roles,
                "name": o.get("name"),
                "party": party,
                "phones": phones,
                "urls": urls,
                "photo": photo,
                "twitter": twitter,
                "facebook": fb,
                "emails": o.get("emails") or [],
                "address": o.get("address") or [],
            })
    return results

def pick_federal(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    senators = []
    house = []
    for r in results:
        rs = [s.lower() for s in (r.get("roles") or [])]
        lv = [l.lower() for l in (r.get("levels") or [])]
        office = (r.get("office") or "").lower()

        # US Senators
        if "legislatorupperbody" in rs or "upper" in office or "senate" in office:
            if "country" in lv or "national" in lv or "us" in office:
                senators.append(r)
        # US House
        if "legislatorlowerbody" in rs or "house" in office:
            if "country" in lv or "national" in lv or "us" in office:
                house.append(r)
    return {"senators": senators, "representatives": house}

# =========================
# Routes
# =========================
@app.get("/")
def home():
    return {"ok": True, "service": "EagleReach API (Google Civic)"}

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}

@app.get("/civic")
async def civic(
    address: str = Query(..., description="US address or ZIP (e.g., 45220)"),
    include_state_leg: int = Query(0, description="1 to query OpenStates for active state legislators"),
    debug: int = Query(0, description="1 to include upstream status"),
):
    if ZIP_RE.match(address):
        # Normalize & add city/state via zippopotam (nice to have; not required)
        loc = await zippopotam_info(address)
        normalized_address = f"{loc['city']}, {loc['state']} {loc['zip']}"
        state_abbr = loc["state"]
    else:
        normalized_address = address
        # do the best to guess state from the string (fallback)
        m = re.search(r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b", address, re.I)
        state_abbr = m.group(1).upper() if m else None

    if not GCIVIC_API_KEY:
        raise HTTPException(status_code=400, detail="GCIVIC_API_KEY not set on the server.")

    upstream = {}

    # --- Google Civic Info ---
    civic_params = {
        "key": GCIVIC_API_KEY,
        "address": normalized_address,
        "levels": "country",  # federal only; remove to get all levels
    }
    civic_data = await fetch_json(GCIVIC_REPS_URL, params=civic_params, cache_key=f"civic:{normalized_address}")
    offices = civic_data.get("offices") or []
    officials = civic_data.get("officials") or []
    flattened = gcivic_office_map(offices, officials)
    fed = pick_federal(flattened)

    upstream["google_civic"] = f"ok: off={len(offices)} officials={len(officials)} flat={len(flattened)}"

    # --- Optionally: OpenStates (state legislators) ---
    state_legislators: List[Dict[str, Any]] = []
    if include_state_leg and state_abbr and OPENSTATES_API_KEY:
        try:
            os_params = {
                # filter to current, active state legislators
                "jurisdiction": state_abbr,          # v3 supports state abbreviations
                "classification": "legislator",
                "active": "true",
                "per_page": "50",
            }
            headers = {"X-API-KEY": OPENSTATES_API_KEY}
            os_data = await fetch_json(OPENSTATES_PEOPLE_URL, params=os_params, headers=headers,
                                       cache_key=f"openstates:{state_abbr}")
            for p in os_data.get("results", []):
                state_legislators.append({
                    "name": p.get("name"),
                    "party": p.get("party"),
                    "chamber": (p.get("chamber") or p.get("current_role", {}).get("chamber")),
                    "district": (p.get("district") or p.get("current_role", {}).get("district")),
                    "links": p.get("links") or [],
                    "email": p.get("email"),
                })
            upstream["openstates"] = f"ok({len(state_legislators)})"
        except Exception as e:
            upstream["openstates"] = f"err({type(e).__name__})"
    elif include_state_leg and not OPENSTATES_API_KEY:
        upstream["openstates"] = "skipped(no API key)"
    else:
        upstream["openstates"] = "skipped"

    payload = {
        "input_address": normalized_address,
        "federal": fed,                       # { senators: [...], representatives: [...] }
        "all_civic": flattened,               # everything returned at country level
        "state_legislators": state_legislators or None,
        "sources": {
            "federal": "Google Civic Information API",
            "state_legislators": "OpenStates (optional)",
        }
    }

    if debug:
        payload["debug"] = upstream

    return payload

# =========================
# Error handler
# =========================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("UNCAUGHT ERROR:", repr(exc))
    return JSONResponse(status_code=500, content={"error": "internal_server_error", "detail": str(exc)})

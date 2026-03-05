# backend/main.py
# EagleReach – Free civic info stack

from __future__ import annotations

import os
import re
import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
import xml.etree.ElementTree as ET

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


# --------------------------------
# Config
# --------------------------------

API_TIMEOUT = float(os.getenv("API_TIMEOUT", "10"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))

ZIP_RE = re.compile(r"^\d{5}$")

STATE_ABBR = set("""
AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split())


# --------------------------------
# External APIs
# --------------------------------

ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"

FCC_AREAS_URL = "https://geo.fcc.gov/api/census/area?lat={lat}&lon={lon}&format=json"

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

LEGIS_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.yaml"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


# --------------------------------
# App setup
# --------------------------------

app = FastAPI(title="EagleReach Civic API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------
# HTTP Client
# --------------------------------

client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():

    global client

    client = httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers={
            "User-Agent": "EagleReach Civic App - https://vikesh2608.github.io/EagleReach (contact: vikesh2608@gmail.com)"
        },
        follow_redirects=True,
    )


@app.on_event("shutdown")
async def shutdown():

    global client

    if client:
        await client.aclose()


# --------------------------------
# Simple cache
# --------------------------------

_cache: Dict[str, Tuple[float, Any]] = {}


def cache_get(key):

    v = _cache.get(key)

    if not v:
        return None

    ts, data = v

    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None

    return data


def cache_set(key, data):

    _cache[key] = (time.time(), data)


# --------------------------------
# HTTP helpers
# --------------------------------

async def fetch_json(url: str, params=None, headers=None, cache_key=None):

    if cache_key:
        cached = cache_get(cache_key)
        if cached:
            return cached

    r = await client.get(url, params=params, headers=headers)

    if r.status_code != 200:
        raise HTTPException(502, f"Upstream error for {url}: {r.text}")

    data = r.json()

    if cache_key:
        cache_set(cache_key, data)

    return data


async def fetch_text(url, cache_key=None):

    if cache_key:
        cached = cache_get(cache_key)
        if cached:
            return cached

    r = await client.get(url)

    if r.status_code != 200:
        raise HTTPException(502, "Upstream text fetch error")

    text = r.text

    if cache_key:
        cache_set(cache_key, text)

    return text


# --------------------------------
# Data providers
# --------------------------------

async def zippopotam_info(zipcode: str):

    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode), cache_key=f"zip:{zipcode}")

    p = data["places"][0]

    return {
        "zip": zipcode,
        "city": p["place name"],
        "state": p["state abbreviation"],
        "state_full": p["state"],
        "lat": float(p["latitude"]),
        "lon": float(p["longitude"]),
    }


async def fcc_district(lat: float, lon: float):

    data = await fetch_json(FCC_AREAS_URL.format(lat=lat, lon=lon))

    res = data["results"][0]

    return {
        "state": res.get("state_code"),
        "district": str(res.get("Congressional District")).zfill(2),
    }


async def nominatim_reverse(lat: float, lon: float):

    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": 10,
        "addressdetails": 1,
    }

    headers = {
        "User-Agent": "EagleReach Civic App - https://vikesh2608.github.io/EagleReach (contact: vikesh2608@gmail.com)"
    }

    data = await fetch_json(
        NOMINATIM_REVERSE,
        params=params,
        headers=headers,
        cache_key=f"rev:{round(lat,4)}:{round(lon,4)}",
    )

    addr = data.get("address", {})

    return {
        "zip": (addr.get("postcode") or "").split("-")[0],
        "city": addr.get("city") or addr.get("town") or addr.get("village"),
        "state": addr.get("state_code"),
        "state_full": addr.get("state"),
    }


async def load_legislators():

    text = await fetch_text(LEGIS_URL, cache_key="legis_yaml")

    return yaml.safe_load(text)


# --------------------------------
# Routes
# --------------------------------

@app.get("/")
def home():
    return {"service": "EagleReach API"}


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/revgeo")
async def revgeo(lat: float, lon: float):

    loc = await nominatim_reverse(lat, lon)

    if not loc["zip"]:
        raise HTTPException(404, "ZIP not found")

    try:

        fcc = await fcc_district(lat, lon)

        loc["district"] = fcc["district"]

    except:
        pass

    return loc


@app.get("/officials")
async def officials(zip: str = Query(...)):

    if not ZIP_RE.match(zip):
        raise HTTPException(400, "Invalid ZIP")

    loc = await zippopotam_info(zip)

    fcc = await fcc_district(loc["lat"], loc["lon"])

    state = fcc["state"]

    data = await load_legislators()

    senators = []
    rep = None

    for p in data:

        t = p["terms"][-1]

        if t["type"] == "sen" and t["state"] == state:

            senators.append({
                "name": p["name"]["official_full"],
                "party": t["party"],
                "website": t.get("url"),
                "phone": t.get("phone"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg",
            })

        if t["type"] == "rep" and t["state"] == state and str(t["district"]) == str(int(fcc["district"])):

            rep = {
                "name": p["name"]["official_full"],
                "party": t["party"],
                "website": t.get("url"),
                "phone": t.get("phone"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg",
            }

    return {
        "location": {**loc, "district": fcc["district"]},
        "officials": {
            "senators": senators,
            "representative": rep,
        },
    }


# --------------------------------
# Global error handler
# --------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):

    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)},
    )

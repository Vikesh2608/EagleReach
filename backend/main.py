# backend/main.py
# EagleReach Civic API

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx
import yaml

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


# -----------------------------
# Configuration
# -----------------------------

API_TIMEOUT = float(os.getenv("API_TIMEOUT", "10"))

ZIP_RE = re.compile(r"^\d{5}$")

ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"

FCC_AREAS_URL = "https://geo.fcc.gov/api/census/area?lat={lat}&lon={lon}&format=json"

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

LEGIS_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.yaml"


# -----------------------------
# FastAPI App
# -----------------------------

app = FastAPI(title="EagleReach Civic API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://vikesh2608.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():

    global client

    client = httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers={
            "User-Agent": "EagleReach Civic App - https://vikesh2608.github.io/EagleReach (contact: vikesh2608@gmail.com)"
        }
    )


@app.on_event("shutdown")
async def shutdown():

    global client

    if client:
        await client.aclose()


# -----------------------------
# Helpers
# -----------------------------

async def fetch_json(url: str, params=None):

    r = await client.get(url, params=params)

    if r.status_code != 200:
        raise HTTPException(502, f"Upstream error: {url}")

    return r.json()


# -----------------------------
# Data Sources
# -----------------------------

async def zippopotam_info(zipcode: str):

    data = await fetch_json(ZIPPOTAM_URL.format(zip=zipcode))

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

    try:
        data = await fetch_json(
            FCC_AREAS_URL.format(lat=lat, lon=lon)
        )

        res = data.get("results", [{}])[0]

        return {
            "state": res.get("state_code"),
            "district": res.get("Congressional District"),
        }

    except:
        return {
            "state": None,
            "district": None
        }


async def nominatim_reverse(lat: float, lon: float):

    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": 10,
        "addressdetails": 1
    }

    data = await fetch_json(NOMINATIM_REVERSE, params=params)

    addr = data.get("address", {})

    return {
        "zip": (addr.get("postcode") or "").split("-")[0],
        "city": addr.get("city") or addr.get("town") or addr.get("village"),
        "state": addr.get("state_code"),
        "state_full": addr.get("state")
    }


async def load_legislators():

    r = await client.get(LEGIS_URL)

    if r.status_code != 200:
        raise HTTPException(502, "Could not load legislators data")

    return yaml.safe_load(r.text)


# -----------------------------
# Routes
# -----------------------------

@app.get("/")
def home():
    return {"service": "EagleReach Civic API"}


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/revgeo")
async def revgeo(lat: float, lon: float):

    loc = await nominatim_reverse(lat, lon)

    try:

        fcc = await fcc_district(lat, lon)

        loc["district"] = fcc.get("district")

        if not loc.get("state"):
            loc["state"] = fcc.get("state")

    except:
        pass

    return loc

@app.get("/officials")
async def officials(zip: str = Query(...)):

    if not ZIP_RE.match(zip):
        raise HTTPException(400, "Invalid ZIP")

    loc = await zippopotam_info(zip)

    state = loc["state"]

    district = None

    try:
        fcc = await fcc_district(loc["lat"], loc["lon"])
        district = fcc.get("district")
    except:
        pass

    data = await load_legislators()

    senators = []
    rep = None

    for p in data:

        terms = p.get("terms", [])

        if not terms:
            continue

        t = terms[-1]

        if t.get("type") == "sen" and t.get("state") == state:

            senators.append({
                "name": p["name"]["official_full"],
                "party": t.get("party"),
                "website": t.get("url"),
                "phone": t.get("phone"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
            })

        if (
            t.get("type") == "rep"
            and t.get("state") == state
            and district
            and str(t.get("district")) == str(district)
        ):

            rep = {
                "name": p["name"]["official_full"],
                "party": t.get("party"),
                "website": t.get("url"),
                "phone": t.get("phone"),
                "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
            }

    return {
        "location": {
            "zip": zip,
            "city": loc["city"],
            "state": state,
            "district": district
        },
        "officials": {
            "senators": senators,
            "representative": rep
        }
    }



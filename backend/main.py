from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import urllib.parse as _u

app = FastAPI()

# Allow GitHub Pages + local dev
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

ZIPPOP = "https://api.zippopotam.us/us"
FCC = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

@app.get("/health")
def health():
    return {"ok": True, "sources": ["zippopotam.us", "FCC Census Block", "GovTrack", "Wikidata"]}

def _esc(s):
    return (s or "").strip()

async def fetch_zip(zip_code:str):
    """ZIP -> {state_abbr, state_name, place_name, lat, lon} via Zippopotam.us"""
    url = f"{ZIPPOP}/{_esc(zip_code)}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        if r.status_code == 404:
            raise HTTPException(404, f"ZIP {zip_code} not found")
        r.raise_for_status()
        j = r.json()
        place = j["places"][0]
        return {
            "state_abbr": _esc(place["state abbreviation"]),
            "state_name": _esc(place["state"]),
            "place_name": _esc(place["place name"]),
            "lat": float(place["latitude"]),
            "lon": float(place["longitude"]),
        }

async def fetch_cd(lat:float, lon:float):
    """lat/lon -> congressional district number via FCC"""
    params = {
        "latitude": lat,
        "longitude": lon,
        "format": "json",
        "showall": "false",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(FCC, params=params)
        r.raise_for_status()
        j = r.json()
        cd = j.get("County", {}).get("FIPS")  # not used
        cd_obj = j.get("CongressionalDistrict")
        num = None
        if isinstance(cd_obj, dict):
            num = _esc(cd_obj.get("FIPS") or cd_obj.get("code") or cd_obj.get("name") or "")
            # FCC returns numeric string like "13" in "code"
            num = _esc(cd_obj.get("code") or "").lstrip("0") or None
        return {"district": num}

async def fetch_senators(state_abbr:str):
    """State -> current U.S. Senators via GovTrack"""
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "senator", "state": state_abbr.upper()}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {})
            rows.append({
                "name": p.get("name") or p.get("name_long"),
                "office": "United States Senator",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],  # GovTrack doesn't give public emails
                "urls": [it.get("website")] if it.get("website") else [p.get("link")] if p.get("link") else [],
            })
        return rows

async def fetch_rep(state_abbr:str, district:str|None):
    """State+District -> current U.S. Representative via GovTrack"""
    if not district:
        return []
    url = f"{GOVTRACK}/role"
    params = {
        "current": "true",
        "role_type": "representative",
        "state": state_abbr.upper(),
        "district": district,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {})
            rows.append({
                "name": p.get("name") or p.get("name_long"),
                "office": f"United States Representative (CD {district})",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],
                "urls": [it.get("website")] if it.get("website") else [p.get("link")] if p.get("link") else [],
            })
        return rows

async def fetch_mayor(place_name:str, state_name:str):
    """
    Best-effort: current mayor via Wikidata (city + state).
    Returns at most one item; if no result, returns [].
    """
    city = _esc(place_name)
    st  = _esc(state_name)
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
    async with httpx.AsyncClient(timeout=20, headers={"Accept":"application/sparql-results+json","User-Agent":"EagleReach/1.0"}) as client:
        r = await client.get(WIKIDATA_SPARQL, params={"query": q})
        if r.status_code != 200:
            return []
        j = r.json()
        b = j.get("results", {}).get("bindings", [])
        if not b:
            return []
        name = b[0].get("personLabel", {}).get("value")
        site = b[0].get("website", {}).get("value")
        if not name:
            return []
        return [{
            "name": name,
            "office": "Mayor",
            "party": None,
            "phones": [],
            "emails": [],
            "urls": [site] if site else [],
        }]

def normalize(rows):
    """Ensure consistent schema and order: Senators, Rep, Mayor, then others."""
    def w(row):
        off = (row.get("office") or "").lower()
        if "united states senator" in off: return 0
        if "united states representative" in off: return 1
        if "mayor" in off: return 2
        return 9
    return sorted(rows, key=w)

@app.get("/officials")
async def officials(zip: str = Query(..., min_length=5, max_length=10)):
    """
    Open-data pipeline:
    - ZIP -> (state_abbr, state_name, place_name, lat, lon) via Zippopotam.us
    - lat/lon -> congressional district via FCC
    - GovTrack -> US Senators & US Representative (live)
    - Wikidata -> Mayor (best-effort)
    """
    try:
        z = await fetch_zip(zip)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"ZIP lookup failed: {e}")

    state_abbr, state_name, place = z["state_abbr"], z["state_name"], z["place_name"]
    lat, lon = z["lat"], z["lon"]

    # Fetch CD + officials concurrently
    cd_task = asyncio.create_task(fetch_cd(lat, lon))
    sens_task = asyncio.create_task(fetch_senators(state_abbr))
    mayor_task = asyncio.create_task(fetch_mayor(place, state_name))

    cd = await cd_task
    district = cd.get("district")

    rep_rows = await fetch_rep(state_abbr, district)
    sen_rows, mayor_rows = await asyncio.gather(sens_task, mayor_task)

    rows = normalize(sen_rows + rep_rows + mayor_rows)

    return {
        "zip": zip,
        "state": {"abbr": state_abbr, "name": state_name},
        "place": place,
        "district": district,
        "officials": rows,
        "sources": {
            "zip": "zippopotam.us",
            "district": "FCC Census Block",
            "congress": "GovTrack",
            "mayor": "Wikidata (best-effort)",
        }
    }

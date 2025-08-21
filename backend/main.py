from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx, asyncio, datetime
from typing import Optional

app = FastAPI(title="EagleReach Backend (Open Data)")

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

# Open-data sources (no API keys)
ZIPPOP = "https://api.zippopotam.us/us"
FCC = "https://geo.fcc.gov/api/census/block/find"
GOVTRACK = "https://www.govtrack.us/api/v2"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

def _esc(s): return (s or "").strip()

@app.get("/health")
def health():
    return {
        "ok": True,
        "backend_version": "1.0.0-open-data",
        "sources": ["zippopotam.us", "FCC Census Block", "GovTrack", "Wikidata"],
    }

async def fetch_zip(zip_code:str):
    """ZIP -> {state_abbr, state_name, place_name, lat, lon}"""
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
    """lat/lon -> congressional district number (string)"""
    params = {"latitude": lat, "longitude": lon, "format": "json", "showall": "false"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(FCC, params=params)
        r.raise_for_status()
        j = r.json()
        cd_obj = j.get("CongressionalDistrict")
        num: Optional[str] = None
        if isinstance(cd_obj, dict):
            num = _esc(cd_obj.get("code") or "").lstrip("0") or None
        return {"district": num}

def fallback_search_url(name:str, state_abbr:str, office:str):
    q = f"{name} {state_abbr} {office} official site"
    return f"https://www.google.com/search?q={httpx.QueryParams({'q': q})['q']}"

async def fetch_senators(state_abbr:str):
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "senator", "state": state_abbr.upper()}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {})
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link")
            if not website:
                website = fallback_search_url(name, state_abbr, "United States Senator")
            rows.append({
                "name": name,
                "office": "United States Senator",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],
                "urls": [website] if website else [],
            })
        return rows

async def fetch_rep(state_abbr:str, district: Optional[str]):
    if not district: return []
    url = f"{GOVTRACK}/role"
    params = {"current": "true", "role_type": "representative", "state": state_abbr.upper(), "district": district}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        j = r.json()
        rows = []
        for it in j.get("objects", []):
            p = it.get("person", {})
            name = p.get("name") or p.get("name_long")
            website = it.get("website") or p.get("link")
            if not website:
                website = fallback_search_url(name, state_abbr, f"United States Representative CD {district}")
            rows.append({
                "name": name,
                "office": f"United States Representative (CD {district})",
                "party": it.get("party"),
                "phones": [it.get("phone")] if it.get("phone") else [],
                "emails": [],
                "urls": [website] if website else [],
            })
        return rows

async def fetch_mayor(place_name:str, state_name:str):
    """
    Best-effort Mayor from Wikidata. If no website in Wikidata, add a
    Google 'official site' search link. Returns 0..1 row.
    """
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
        url = site or fallback_search_url(name, st, "Mayor")
        if not name:
            return []
        return [{
            "name": name,
            "office": "Mayor",
            "party": None,
            "phones": [],
            "emails": [],
            "urls": [url] if url else [],
        }]

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

    cd_task    = asyncio.create_task(fetch_cd(lat, lon))
    senate_task= asyncio.create_task(fetch_senators(state_abbr))
    mayor_task = asyncio.create_task(fetch_mayor(place, state_name))

    cd = await cd_task
    district = cd.get("district")
    rep_rows  = await fetch_rep(state_abbr, district)
    sen_rows, mayor_rows = await asyncio.gather(senate_task, mayor_task)

    rows = normalize(sen_rows + rep_rows + mayor_rows)

    return {
        "zip": zip,
        "state": {"abbr": state_abbr, "name": state_name},
        "place": place,
        "district": district,
        "officials": rows,
        "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sources": {
            "zip": "zippopotam.us",
            "district": "FCC Census Block",
            "congress": "GovTrack",
            "mayor": "Wikidata (best-effort, with search fallback)"
        }
    }

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx, asyncio, datetime, time, math
from typing import Optional, Dict, Any, Tuple

app = FastAPI(title="EagleReach Backend")

ALLOWED = [
    "https://vikesh2608.github.io",
    "https://vikesh2608.github.io/EagleReach/",
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
UA = {"User-Agent":"EagleReach/1.2 (+contact: vikebairam@gmail.com)"}

def _esc(s): return (s or "").strip()

# tiny TTL cache
_cache: Dict[str, Tuple[float, Any]] = {}
def cache_get(k): 
    v=_cache.get(k); 
    return None if not v or time.time()>v[0] else v[1]
def cache_set(k,data,ttl=600): _cache[k]=(time.time()+ttl,data)

@app.get("/health")
def health():
    return {"ok":True,"backend_version":"1.2.0-open-data",
            "sources":["zippopotam.us","FCC Census Block","GovTrack","Wikidata","Open‑Meteo"]}

async def fetch_zip(zip_code: str):
    url=f"{ZIPPOP}/{_esc(zip_code)}"
    ck=f"zip:{zip_code}"
    c=cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=15,headers=UA) as client:
        r=await client.get(url)
        if r.status_code==404: raise HTTPException(404, f"ZIP {zip_code} not found")
        r.raise_for_status()
        j=r.json()
        p=j["places"][0]
        out={"state_abbr":_esc(p["state abbreviation"]),
             "state_name":_esc(p["state"]),
             "place_name":_esc(p["place name"]),
             "lat":float(p["latitude"]),
             "lon":float(p["longitude"])}
        cache_set(ck,out,3600)
        return out

async def fetch_cd(lat:float, lon:float):
    params={"latitude":lat,"longitude":lon,"format":"json","showall":"false"}
    ck=f"cd:{lat:.4f},{lon:.4f}"
    c=cache_get(ck)
    if c: return c
    async with httpx.AsyncClient(timeout=15,headers=UA) as client:
        r=await client.get(FCC,params=params); r.raise_for_status()
        j=r.json(); num=None
        cd=j.get("CongressionalDistrict")
        if isinstance(cd,dict): num=_esc(cd.get("code") or "").lstrip("0") or None
        out={"district":num}
        cache_set(ck,out,3600); return out

def fallback_search_url(name, state_abbr, office):
    from urllib.parse import urlencode
    q=f"{name} {state_abbr} {office} official site"
    return "https://www.google.com/search?"+urlencode({"q":q})

async def fetch_senators(state_abbr:str):
    ck=f"sen:{state_abbr}"; c=cache_get(ck); 
    if c: return c
    params={"current":"true","role_type":"senator","state":state_abbr.upper()}
    async with httpx.AsyncClient(timeout=20,headers=UA) as client:
        r=await client.get(f"{GOVTRACK}/role",params=params); r.raise_for_status()
        rows=[]
        for it in r.json().get("objects",[]):
            p=it.get("person",{})
            name=p.get("name") or p.get("name_long")
            website=it.get("website") or p.get("link") or fallback_search_url(name,state_abbr,"United States Senator")
            rows.append({
                "name":name,"office":"United States Senator","party":it.get("party"),
                "phones":[it.get("phone")] if it.get("phone") else [], "emails":[],
                "urls":[website] if website else []
            })
        cache_set(ck,rows,3600); return rows

async def fetch_rep(state_abbr:str, district:Optional[str]):
    if not district: return []
    ck=f"rep:{state_abbr}:{district}"; c=cache_get(ck); 
    if c: return c
    params={"current":"true","role_type":"representative","state":state_abbr.upper(),"district":district}
    async with httpx.AsyncClient(timeout=20,headers=UA) as client:
        r=await client.get(f"{GOVTRACK}/role",params=params); r.raise_for_status()
        rows=[]
        for it in r.json().get("objects",[]):
            p=it.get("person",{})
            name=p.get("name") or p.get("name_long")
            website=it.get("website") or p.get("link") or fallback_search_url(name,state_abbr,f"United States Representative CD {district}")
            rows.append({
                "name":name,"office":f"United States Representative (CD {district})","party":it.get("party"),
                "phones":[it.get("phone")] if it.get("phone") else [], "emails":[],
                "urls":[website] if website else []
            })
        cache_set(ck,rows,3600); return rows

async def fetch_mayor(place_name:str, state_name:str):
    city=_esc(place_name); st=_esc(state_name)
    if not city or not st: return []
    ck=f"mayor:{city},{st}"; c=cache_get(ck)
    if c is not None: return c
    q=f"""
    SELECT ?personLabel ?website WHERE {{
      ?city rdfs:label "{city}"@en ; wdt:P17 wd:Q30 ; wdt:P131* ?state ; wdt:P31/wdt:P279* wd:Q515 .
      ?state rdfs:label "{st}"@en . ?city wdt:P6 ?person .
      OPTIONAL {{ ?person wdt:P856 ?website. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 1"""
    async with httpx.AsyncClient(timeout=20,headers={**UA,"Accept":"application/sparql-results+json"}) as client:
      r=await client.get(WIKIDATA_SPARQL,params={"query":q})
      if r.status_code!=200: cache_set(ck,[],600); return []
      b=r.json().get("results",{}).get("bindings",[])
      if not b: cache_set(ck,[],600); return []
      name=b[0].get("personLabel",{}).get("value")
      site=b[0].get("website",{}).get("value")
      url=site or fallback_search_url(name, st, "Mayor")
      if not name: cache_set(ck,[],600); return []
      out=[{"name":name,"office":"Mayor","party":None,"phones":[],"emails":[],"urls":[url] if url else []}]
      cache_set(ck,out,3600); return out

def normalize(rows):
    def ord(row):
        t=(row.get("office") or "").lower()
        if "united states senator" in t: return 0
        if "representative" in t:     return 1
        if "mayor" in t:               return 2
        return 9
    return sorted(rows, key=ord)

@app.get("/officials")
async def officials(zip: str = Query(..., min_length=5, max_length=10)):
    z=await fetch_zip(zip)
    lat,lon=z["lat"],z["lon"]
    cd_task=asyncio.create_task(fetch_cd(lat,lon))
    sen_task=asyncio.create_task(fetch_senators(z["state_abbr"]))
    mayor_task=asyncio.create_task(fetch_mayor(z["place_name"],z["state_name"]))
    cd=await cd_task; district=cd.get("district")
    rep_rows=await fetch_rep(z["state_abbr"],district)
    sen_rows,mayor_rows=await asyncio.gather(sen_task,mayor_task)
    rows=normalize(sen_rows+rep_rows+mayor_rows)
    return {
      "zip":zip,
      "state":{"abbr":z["state_abbr"],"name":z["state_name"]},
      "place":z["place_name"],
      "location":{"lat":lat,"lon":lon},
      "district":district,
      "officials":rows
    }

# ---- Weather (Open‑Meteo) with small summaries for icons
@app.get("/weather")
async def weather(zip: str):
    z=await fetch_zip(zip); lat,lon=z["lat"],z["lon"]
    url=("https://api.open-meteo.com/v1/forecast"
         f"?latitude={lat}&longitude={lon}"
         "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
         "&forecast_days=7&timezone=auto")
    async with httpx.AsyncClient(timeout=15,headers=UA) as client:
        r=await client.get(url); r.raise_for_status(); j=r.json()
    daily=j.get("daily",{})
    times=daily.get("time",[])
    tmax=daily.get("temperature_2m_max",[])
    tmin=daily.get("temperature_2m_min",[])
    ppop=daily.get("precipitation_probability_max",[])
    codes=daily.get("weathercode",[])
    def summary(code:int)->str:
        # open-meteo WMO codes
        if code in (0,1): return "Clear"
        if code in (2,3): return "Cloudy"
        if 45<=code<=48:  return "Fog"
        if 51<=code<=67:  return "Drizzle/Rain"
        if 71<=code<=77:  return "Snow"
        if 80<=code<=82:  return "Rain"
        if 95<=code<=99:  return "Thunder"
        return "Weather"
    out=[]
    for i in range(min(len(times),len(tmax),len(tmin),len(ppop),len(codes))):
        out.append({"date":times[i],"tmax_c":tmax[i],"tmin_c":tmin[i],
                    "precip_pct":ppop[i],"summary":summary(int(codes[i]))})
    return {"zip":zip,"place":z["place_name"],"state":z["state_abbr"],
            "lat":lat,"lon":lon,"days":out,
            "updated_at":datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z"}

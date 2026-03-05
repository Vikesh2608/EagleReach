from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import yaml

app = FastAPI()

app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
allow_methods=["*"],
allow_headers=["*"],
)

ZIPPOTAM_URL = "https://api.zippopotam.us/us/{zip}"
LEGIS_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.yaml"

client = httpx.AsyncClient()

async def get_zip_info(zip):
r = await client.get(ZIPPOTAM_URL.format(zip=zip))

```
if r.status_code != 200:
    raise HTTPException(status_code=404, detail="ZIP not found")

data = r.json()
place = data["places"][0]

return {
    "city": place["place name"],
    "state": place["state abbreviation"]
}
```

async def load_legislators():
r = await client.get(LEGIS_URL)
return yaml.safe_load(r.text)

@app.get("/health")
def health():
return {"ok": True}

@app.get("/officials")
async def officials(zip: str):

```
loc = await get_zip_info(zip)
state = loc["state"]

data = await load_legislators()

senators = []
rep = None

for p in data:

    term = p["terms"][-1]

    if term["type"] == "sen" and term["state"] == state:
        senators.append({
            "name": p["name"]["official_full"],
            "party": term["party"],
            "website": term.get("url"),
            "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
        })

    if term["type"] == "rep" and term["state"] == state:
        rep = {
            "name": p["name"]["official_full"],
            "party": term["party"],
            "website": term.get("url"),
            "photo": f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
        }

return {
    "location": {
        "zip": zip,
        "city": loc["city"],
        "state": state
    },
    "officials": {
        "senators": senators,
        "representative": rep
    }
}
```

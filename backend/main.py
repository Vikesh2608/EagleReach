from fastapi import FastAPI,HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx,yaml

app = FastAPI()

app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
allow_methods=["*"],
allow_headers=["*"],
)

ZIPPOTAM_URL="https://api.zippopotam.us/us/{zip}"
LEGIS_URL="https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.yaml"

client=httpx.AsyncClient()

async def zip_info(zip):

r=await client.get(ZIPPOTAM_URL.format(zip=zip))

if r.status_code!=200:
raise HTTPException(404,"ZIP not found")

data=r.json()

p=data["places"][0]

return{
"city":p,
"state":p,
"lat":p,
"lon":p
}

async def load_legislators():

r=await client.get(LEGIS_URL)

return yaml.safe_load(r.text)

@app.get("/health")
def health():
return{"ok":True}

@app.get("/officials")
async def officials(zip:str):

loc=await zip_info(zip)

state=loc["state"]

data=await load_legislators()

senators=[]
rep=None

for p in data:

t=p["terms"][-1]

if t["type"]=="sen" and t["state"]==state:

senators.append({
"name":p["official_full"],
"party":t,
"website":t.get("url"),
"photo":f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
})

if t["type"]=="rep" and t["state"]==state:

rep={
"name":p["official_full"],
"party":t,
"website":t.get("url"),
"photo":f"https://theunitedstates.io/images/congress/450x550/{p['id']['bioguide']}.jpg"
}

return{
"location":{
"zip":zip,
"city":loc,
"state":state
},
"officials":{
"senators":senators,
"representative":rep
}
}

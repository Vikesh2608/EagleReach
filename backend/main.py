# backend/main.py
import os
import re
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================
# ENV & CONFIG
# ============================================================
OPENSTATES_API_KEY = os.getenv("OPENSTATES_API_KEY", "").strip()
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "https://vikesh2608.github.io,https://vikesh2608.github.io/EagleReach/").split(",")
    if o.strip()
]
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

if not OPENSTATES_API_KEY and not DEMO_MODE:
    # We don't crash here so /health still works; /ask will error if called.
    print("WARNING: OPENSTATES_API_KEY not set; /ask will fail unless DEMO_MODE=true")

# Simple UA for Nominatim per their policy
NOMINATIM_HEADERS = {
    "User-Agent": "EagleReach/1.0 (contact: example@example.com)"
}

# ============================================================
# APP
# ============================================================
app = FastAPI(title="EagleReach Backend (OpenStates)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MODELS
# ============================================================
class AskRequest(BaseModel):
    address: str


# ============================================================
# HELPERS
# ============================================================
ZIP_RE = re.compile(r"^\s*\d{5}\s*$")

def geocode(address: str) -> Optional[Dict[str, float]]:
    """
    Geocode using OpenStreetMap Nominatim (no API key).
    Returns dict with lat/lng (floats) or None.
    """
    q = address.strip()
    params = {
        "format": "jsonv2",
        "limit": 1,
    }

    # If just a 5-digit ZIP, bias to US
    if ZIP_RE.match(q):
        params.update({"postalcode": q, "countrycodes": "us"})
        url = "https://nominatim.openstreetmap.org/search"
    else:
        params.update({"q": q})
        url = "https://nominatim.openstreetmap.org/search"

    try:
        r = requests.get(url, params=params, headers=NOMINATIM_HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        item = data[0]
        return {"lat": float(item["lat"]), "lng": float(item["lon"])}
    except Exception as e:
        print("Geocode error:", e)
        return None


def call_openstates_people_geo(lat: float, lng: float) -> Dict[str, Any]:
    url = "https://v3.openstates.org/people.geo"
    headers = {"X-API-KEY": OPENSTATES_API_KEY}
    params = {"lat": lat, "lng": lng}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    if r.status_code == 401 or r.status_code == 403:
        raise HTTPException(status_code=502, detail="OpenStates auth failed. Check API key.")
    r.raise_for_status()
    return r.json()


def normalize_officials(os_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Map OpenStates people.geo results into a frontend-friendly list.
    """
    results = os_json.get("results", []) or []
    officials: List[Dict[str, Any]] = []

    for p in results:
        name = p.get("name") or p.get("given_name") or "Unknown"
        party = p.get("party") or p.get("current_party") or ""
        current_role = p.get("current_role") or {}  # dict
        title = (current_role or {}).get("title") or ""
        district = (current_role or {}).get("district")
        jurisdiction = (p.get("jurisdiction") or {}).get("name") or ""
        office_text = f"{jurisdiction} — {title}" if title else jurisdiction
        if district:
            office_text += f" (District {district})"

        # Collect website links
        urls = []
        if p.get("links"):
            for l in p["links"]:
                u = l.get("url")
                if u and u not in urls:
                    urls.append(u)
        if p.get("openstates_url") and p["openstates_url"] not in urls:
            urls.append(p["openstates_url"])

        # Phones & emails from offices
        phones, emails = [], []
        for off in p.get("offices") or []:
            ph = off.get("voice") or off.get("phone")
            em = off.get("email")
            if ph and ph not in phones:
                phones.append(ph)
            if em and em not in emails:
                emails.append(em)

        officials.append(
            {
                "name": name,
                "party": party,
                "office": office_text.strip(" —"),
                "urls": urls[:4],
                "phones": phones[:4],
                "emails": emails[:4],
            }
        )

    return officials


# ============================================================
# ROUTES
# ============================================================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask")
def ask(payload: AskRequest):
    if DEMO_MODE:
        # Minimal demo response if needed
        return {
            "officials": [
                {
                    "name": "Demo Official",
                    "party": "Independent",
                    "office": "Demo Jurisdiction — Representative",
                    "urls": ["https://example.com"],
                    "phones": ["(555) 555-5555"],
                    "emails": ["demo@example.com"],
                }
            ]
        }

    if not OPENSTATES_API_KEY:
        raise HTTPException(status_code=500, detail="OPENSTATES_API_KEY is not configured on the server.")

    addr = (payload.address or "").strip()
    if not addr:
        raise HTTPException(status_code=400, detail="address is required")

    # 1) geocode
    coords = geocode(addr)
    if not coords:
        raise HTTPException(status_code=404, detail="Could not geocode that address/ZIP.")

    # 2) call OpenStates
    try:
        raw = call_openstates_people_geo(coords["lat"], coords["lng"])
    except requests.HTTPError as e:
        print("OpenStates HTTP error:", e.response.text if e.response is not None else e)
        raise HTTPException(status_code=502, detail="Upstream civic data lookup failed.")
    except Exception as e:
        print("OpenStates error:", e)
        raise HTTPException(status_code=502, detail="Upstream civic data lookup failed.")

    # 3) map & return
    officials = normalize_officials(raw)
    return {"officials": officials}


# ============================================================
# LOCAL DEV ENTRY
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)

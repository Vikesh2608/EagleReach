from fastapi.middleware.cors import CORSMiddleware
# TODO: replace "*" with your real frontend origin when you have it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # e.g., ["https://your-frontend.app"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# backend/main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List

# ✅ our free provider (Census Geocoder + congress-legislators JSON)
from backend.providers.free_civic import (
    get_federal_officials,
    CivicLookupError,
    Official,
)

app = FastAPI(title="EagleReach API", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- /ask (address -> officials) ----------
class AskRequest(BaseModel):
    # Use a full street address for best accuracy; ZIP-only will be added next step
    address: str


class AskResponse(BaseModel):
    officials: List[Official]


@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest):
    try:
        addr = payload.address.strip()
        if addr.isdigit() and len(addr) == 5:
            # It's a ZIP code
            from backend.providers.free_civic import address_from_zip
            addr = await address_from_zip(addr)
        officials = await get_federal_officials(addr)
        return AskResponse(officials=officials)
    except CivicLookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Lookup failed")


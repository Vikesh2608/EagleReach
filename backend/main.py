# backend/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

from backend.providers.free_civic import (
    get_federal_officials,
    CivicLookupError,
    Official,
)

# 1) Instantiate app FIRST
app = FastAPI(title="EagleReach API", version="1.0.0")

# 2) Then add CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # replace with your frontend origin later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3) Routes
@app.get("/health")
def health():
    return {"status": "ok"}

class AskRequest(BaseModel):
    address: str  # ZIP or full street address

class AskResponse(BaseModel):
    officials: List[Official]

@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest):
    try:
        addr = payload.address.strip()
        officials = await get_federal_officials(addr)
        return AskResponse(officials=officials)
    except CivicLookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Lookup failed")

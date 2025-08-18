# backend/main.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --- robust provider import (works whether root is repo/ or backend/) ---
try:
    from backend.providers.free_civic import get_federal_officials, CivicLookupError
except ModuleNotFoundError:
    from providers.free_civic import get_federal_officials, CivicLookupError

# --- logging / debug ---
logger = logging.getLogger("eaglereach")
logging.basicConfig(level=logging.INFO)
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# --- CORS ---
GITHUB_USERNAME = "Vikesh2608"
GITHUB_REPO     = "EagleReach"

default_allowed_origins = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5500",
    f"https://{GITHUB_USERNAME}.github.io",
    f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/",
]
allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
allow_origins: List[str] = (
    [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    if allowed_origins_env else default_allowed_origins
)

app = FastAPI(title="EagleReach API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- models ---
class AskRequest(BaseModel):
    address: str = Field(..., description="ZIP code or full address")

class AskResponse(BaseModel):
    officials: List[Dict[str, Any]]

# --- routes ---
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    try:
        officials = get_federal_officials(payload.address)
        return AskResponse(officials=officials)
    except CivicLookupError as e:
        logger.warning("CivicLookupError for %s: %s", payload.address, e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error in /ask for %s", payload.address)
        if DEBUG:
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

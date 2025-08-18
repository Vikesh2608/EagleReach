# backend/main.py
from __future__ import annotations

import os
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import your provider (already in your repo)
from backend.providers.free_civic import get_federal_officials, CivicLookupError

# ─────────────────────────────────────────────────────────────
# Configure the origins that can call this API from the browser
# ─────────────────────────────────────────────────────────────
# Option A (simple): fill these in and commit
GITHUB_USERNAME = "vikesh2608"       # e.g., "Vikesh2608"
GITHUB_REPO     = "EagleReach"             # e.g., "EagleReach"

# Option B (preferred for production): set ALLOWED_ORIGINS in Render
# as a comma-separated list, e.g.
# "https://Vikesh2608.github.io,https://Vikesh2608.github.io/EagleReach/"
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
    if allowed_origins_env
    else default_allowed_origins
)

# ─────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="EagleReach API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    address: str = Field(..., description="ZIP code or full address")

class AskResponse(BaseModel):
    # We return a list of dicts to avoid tight coupling to the provider model
    officials: List[Dict[str, Any]]

# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    """
    Look up federal officials for the given ZIP/address.
    """
    try:
        officials = get_federal_officials(payload.address)  # returns list[dict]
        return AskResponse(officials=officials)
    except CivicLookupError as e:
        # Something like "No geocoding match for that ZIP"
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # Generic catch-all so we don't leak stack traces
        raise HTTPException(status_code=500, detail="Internal server error")

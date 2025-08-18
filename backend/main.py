# backend/main.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Provider (already in your repo)
from backend.providers.free_civic import get_federal_officials, CivicLookupError

# ─────────────────────────────────────────────────────────────
# Logging + Debug toggle
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("eaglereach")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────
# CORS: allow your GitHub Pages (and optional overrides via env)
# ─────────────────────────────────────────────────────────────
# These can be overridden with ALLOWED_ORIGINS in Render:
# e.g. "https://vikesh2608.github.io,https://vikesh2608.github.io/EagleReach/"
GITHUB_USERNAME = "Vikesh2608"
GITHUB_REPO = "EagleReach"

default_allowed_origins: List[str] = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5500",
    f"https://{GITHUB_USERNAME}.github.io",
    f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/",
]

_allowed_from_env = os.getenv("ALLOWED_ORIGINS")
allow_origins: List[str] = (
    [o.strip() for o in _allowed_from_env.split(",") if o.strip()]
    if _allowed_from_env
    else default_allowed_origins
)

logger.info("CORS allow_origins = %s", allow_origins)

# ─────────────────────────────────────────────────────────────
# FastAPI app
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
    # Use a dict list for flexibility (keeps provider details decoupled)
    officials: List[Dict[str, Any]]

# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Dict[str, str]:
    """Simple health check."""
    return {"status": "ok"}

@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    """
    Look up federal officials for the given ZIP/address.
    Returns a list of officials with optional 'url' fields.
    """
    logger.info("ASK request for address/zip: %s", payload.address)
    try:
        officials = get_federal_officials(payload.address)  # -> List[Dict[str, Any]]
        logger.info("ASK success: %d officials", len(officials))
        return AskResponse(officials=officials)

    except CivicLookupError as e:
        # e.g., "No geocoding match for that ZIP"
        logger.warning("CivicLookupError for %s: %s", payload.address, e)
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        # Generic safety net (so we don't leak internals by default)
        logger.exception("Unhandled error in /ask for %s", payload.address)
        if DEBUG:
            # In debug mode, show the exception type/message in the response
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

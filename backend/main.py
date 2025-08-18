# backend/main.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Your provider module (already in your repo)
from backend.providers.free_civic import get_federal_officials, CivicLookupError


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("eaglereach")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
DEBUG = os.getenv("DEBUG", "false").lower() == "true"


# ─────────────────────────────────────────────────────────────
# CORS: allow your GitHub Pages site + local dev
# You can also override with env var:
#   ALLOWED_ORIGINS="https://vikesh2608.github.io,https://vikesh2608.github.io/EagleReach/"
# ─────────────────────────────────────────────────────────────
default_allowed_origins: List[str] = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    # GitHub Pages (user and project pages)
    "https://vikesh2608.github.io",
    "https://vikesh2608.github.io/EagleReach/",
]

allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
allow_origins: List[str] = (
    [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    if allowed_origins_env
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
    # Use a loose shape to avoid coupling UI to provider internals
    officials: List[Dict[str, Any]]


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    """
    Look up federal officials for the given ZIP/address and return a list
    of dicts containing at least: name, office, and urls (if available).
    """
    logger.info("ASK: address=%s", payload.address)
    try:
        officials = get_federal_officials(payload.address)  # list[dict]
        logger.info("ASK: %d officials returned", len(officials))
        return AskResponse(officials=officials)

    except CivicLookupError as e:
        # Known/expected input issues (bad ZIP, no match, etc.)
        logger.warning("CivicLookupError for '%s': %s", payload.address, e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        # Unknown/unexpected server failure
        logger.exception("Unhandled error in /ask for '%s'", payload.address)
        if DEBUG:
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
        raise HTTPException(status_code=500, detail="Internal server error") from e


# Optional: local run (not used by Render, but handy if you run locally)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )

# backend/main.py
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Your existing provider (keep this import)
from backend.providers.free_civic import get_federal_officials, CivicLookupError

# ─────────────────────────────────────────────────────────────
# Logging & flags
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("eaglereach")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEBUG = os.getenv("DEBUG", "false").lower() == "true"
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────
GITHUB_USERNAME = "vikesh2608"
GITHUB_REPO = "EagleReach"

default_allowed_origins = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5500",
    f"https://{GITHUB_USERNAME}.github.io",
    f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/",
]

allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
ALLOW_ORIGINS: List[str] = (
    [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    if allowed_origins_env
    else default_allowed_origins
)

# ─────────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="EagleReach API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
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
    officials: List[Dict[str, Any]]

# ─────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

# ─────────────────────────────────────────────────────────────
# Demo data (for a smooth public demo)
# ─────────────────────────────────────────────────────────────
DEMO_OFFICIALS = [
    {
        "name": "Richard J. Durbin",
        "office": "US Senator",
        "urls": ["https://www.durbin.senate.gov/"],
    },
    {
        "name": "Tammy Duckworth",
        "office": "US Senator",
        "urls": ["https://www.duckworth.senate.gov/"],
    },
    {
        "name": "Nikki Budzinski",
        "office": "US Representative",
        "urls": ["https://budzinski.house.gov/"],
    },
]

# ─────────────────────────────────────────────────────────────
# /ask
# ─────────────────────────────────────────────────────────────
@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    addr = payload.address.strip()
    logger.info("ASK start address=%s demo=%s", addr, DEMO_MODE)

    # Short-circuit for empty address
    if not addr:
        raise HTTPException(status_code=400, detail="Address/ZIP is required.")

    # Fast demo path: no upstream calls, great for judging
    if DEMO_MODE:
        logger.info("Returning DEMO officials for address=%s", addr)
        return AskResponse(officials=DEMO_OFFICIALS)

    # Real provider path
    try:
        officials = get_federal_officials(addr)  # must return list[dict]
        logger.info("ASK OK address=%s officials=%d", addr, len(officials))
        return AskResponse(officials=officials)

    except CivicLookupError as e:
        # Bad ZIP or no match → user error
        logger.warning("ASK CivicLookupError address=%s: %s", addr, e)
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        # Upstream failure or unexpected bug → log & return a clean 502/500
        logger.exception("ASK error address=%s: %s", addr, e)
        if DEBUG:
            raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Upstream civic data lookup failed.")

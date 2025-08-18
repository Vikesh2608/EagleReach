# backend/main.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Providers
from backend.providers.free_civic import get_federal_officials, CivicLookupError

# ---------- helpers ----------

def as_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}

# Env
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
ALLOW_ORIGINS: List[str] = (
    [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    if allowed_origins_env else default_allowed_origins
)

DEBUG     = as_bool(os.getenv("DEBUG"),     default=False)
DEMO_MODE = as_bool(os.getenv("DEMO_MODE"), default=False)

# Logging
logger = logging.getLogger("eaglereach")
logging.basicConfig(level=logging.INFO)
logger.info("Booting EagleReach | DEBUG=%s DEMO_MODE=%s ALLOW_ORIGINS=%s",
            DEBUG, DEMO_MODE, ALLOW_ORIGINS)

# ---------- app ----------

app = FastAPI(title="EagleReach API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- models ----------

class AskRequest(BaseModel):
    address: str = Field(..., description="ZIP code or full address")

class AskResponse(BaseModel):
    officials: List[Dict[str, Any]]

# ---------- endpoints ----------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

# Temporary endpoint to verify env is picked up in Render
@app.get("/config")
def config() -> Dict[str, Any]:
    return {
        "demo_mode": DEMO_MODE,
        "debug": DEBUG,
        "allow_origins": ALLOW_ORIGINS,
    }

@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    """
    Look up federal officials for the given ZIP/address.
    Falls back to demo officials when DEMO_MODE=true or upstream fails.
    """
    try:
        officials = get_federal_officials(payload.address)
        return AskResponse(officials=officials)

    except CivicLookupError as e:
        logger.warning("CivicLookupError for %s: %s", payload.address, e)
        if DEMO_MODE:
            logger.info("DEMO_MODE on -> returning demo officials for %s", payload.address)
            return AskResponse(officials=[
                {"name": "Senator A (Demo)", "office": "US Senator", "urls": ["https://senate.gov"]},
                {"name": "Senator B (Demo)", "office": "US Senator", "urls": ["https://senate.gov"]},
                {"name": "Rep C (Demo)",     "office": "US Representative", "urls": ["https://house.gov"]},
            ])
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.exception("Unhandled error in /ask for %s", payload.address)
        if DEMO_MODE:
            logger.info("DEMO_MODE on -> returning demo officials after error")
            return AskResponse(officials=[
                {"name": "Senator A (Demo)", "office": "US Senator", "urls": ["https://senate.gov"]},
                {"name": "Senator B (Demo)", "office": "US Senator", "urls": ["https://senate.gov"]},
                {"name": "Rep C (Demo)",     "office": "US Representative", "urls": ["https://house.gov"]},
            ])
        # keep the message explicit so we know it's upstream
        raise HTTPException(status_code=502, detail="Upstream civic data lookup failed.")

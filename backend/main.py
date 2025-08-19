# backend/main.py
from __future__ import annotations

import os
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ✅ IMPORTANT: import via the package path so it works on Render
from backend.providers.free_civic import (
    CivicLookupError,
    address_from_zip,
    get_federal_officials,
)

app = FastAPI(title="EagleReach API", version="1.0.0")

# ---- CORS (for your GitHub Pages frontend) ----
# Set ALLOWED_ORIGINS in Render like:
#   https://vikesh2608.github.io,https://vikesh2608.github.io/EagleReach/
allowed_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "")
    .split(",")
    if o.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class AskRequest(BaseModel):
    address: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/ask")
async def ask(payload: AskRequest) -> dict:
    """
    Accepts either a full address or a 5‑digit ZIP code,
    and returns current federal officials (2 senators + house rep if available).
    """
    try:
        addr = (payload.address or "").strip()
        if not addr:
            raise CivicLookupError("Please provide an address or 5‑digit ZIP.")

        # ZIP convenience: pass ZIP through the geocoder helper
        if addr.isdigit() and len(addr) == 5:
            addr = await address_from_zip(addr)

        officials = await get_federal_officials(addr)

        # pydantic v2 models -> dicts
        return {"officials": [o.model_dump() for o in officials]}

    except (CivicLookupError, httpx.HTTPError) as e:
        # Bad input or upstream error (geocoder/network etc.)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # Unexpected server error
        raise HTTPException(status_code=500, detail="Internal server error") from e


# ---- Local dev entry (ignored by Render) ----
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )

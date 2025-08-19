# backend/main.py
from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

from providers.free_civic import (
    CivicLookupError,
    address_from_zip,
    get_federal_officials,
)

app = FastAPI()


class AskRequest(BaseModel):
    address: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ask")
async def ask(payload: AskRequest):
    try:
        addr = (payload.address or "").strip()
        if not addr:
            raise CivicLookupError("Please provide an address or 5‑digit ZIP.")

        # ZIP-only convenience
        if addr.isdigit() and len(addr) == 5:
            addr = await address_from_zip(addr)

        officials = await get_federal_officials(addr)
        # pydantic v2
        return {"officials": [o.model_dump() for o in officials]}

    except (CivicLookupError, httpx.HTTPError) as e:
        # User/input or upstream error
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # Unexpected error
        raise HTTPException(status_code=500, detail="Internal server error") from e


# --- Local dev entry (Render ignores this, but useful locally) ---
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)

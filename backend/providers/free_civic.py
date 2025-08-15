# backend/providers/free_civic.py
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel

# --- endpoints / data sources (all free, keyless) ---
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"

# --- tiny in-memory cache (address/ZIP -> officials) ---
_CACHE: Dict[str, Tuple[float, List["Official"]]] = {}
_CACHE_TTL = 3600  # 1 hour


class Official(BaseModel):
    level: str              # 'federal'
    office: str             # 'US Senator' | 'US Representative'
    name: str
    party: Optional[str] = None
    state: str
    district: Optional[str] = None
    phones: List[str] = []
    urls: List[str] = []
    photo_url: Optional[str] = None
    ids: Dict[str, Any] = {}


class CivicLookupError(RuntimeError):
    pass


async def _load_legislators() -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(LEGISLATORS_URL)
        r.raise_for_status()
        return r.json()


def _to_official(person: Dict[str, Any], term: Dict[str, Any]) -> Official:
    name = person.get("name", {})
    full = name.get("official_full") or " ".join(
        x for x in [name.get("first"), name.get("middle"), name.get("last")] if x
    ).strip()
    ids = person.get("id", {})
    office = "US Senator" if term.get("type") == "sen" else "US Representative"
    return Official(
        level="federal",
        office=office,
        name=full,
        party=term.get("party"),
        state=term.get("state"),
        district=str(term.get("district")) if "district" in term else None,
        phones=[p for p in [term.get("phone")] if p],
        urls=[u for u in [term.get("url")] if u],
        photo_url=None,
        ids=ids,
    )


async def _census_geocode(address: str) -> Tuple[str, int]:
    """
    Return (state_abbr, district_num). District 0 = at-large.
    ZIP-only inputs are handled via coordinates -> geographies for reliability.
    """
    async with httpx.AsyncClient(timeout=20) as client:

        def extract_state_and_cd(geo: Dict[str, Any]) -> Optional[Tuple[str, int]]:
            state_items = geo.get("States") or []
            if not state_items:
                return None
            state = state_items[0].get("STUSAB")
            if not state:
                return None

            cd_key = next((k for k in geo.keys() if "Congressional District" in k), None)
            if not cd_key:
                district = 0
            else:
                cd_items = geo.get(cd_key) or []
                if not cd_items:
                    district = 0
                else:
                    basename = (cd_items[0].get("BASENAME") or "").strip()
                    if basename.lower().startswith("at"):
                        district = 0
                    else:
                        digits = "".join(ch for ch in basename if ch.isdigit())
                        district = int(digits or "0")
            return state, district

        # ZIP flow → coords → Census geographies/coordinates
        if address.isdigit() and len(address) == 5:
            zr = await client.get(f"https://api.zippopotam.us/us/{address}")
            if zr.status_code != 200:
                raise CivicLookupError(f"ZIP code {address} not found.")
            z = zr.json()
            places = z.get("places") or []
            if not places:
                raise CivicLookupError(f"No place found for ZIP {address}.")
            p = places[0]
            lat = p.get("latitude")
            lng = p.get("longitude")
            if not lat or not lng:
                raise CivicLookupError(f"Coordinates unavailable for ZIP {address}.")

            rev_params = {
                "x": float(lng),  # longitude
                "y": float(lat),  # latitude
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "all",
                "format": "json",
            }
            rr = await client.get(
                "https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
                params=rev_params,
            )
            rr.raise_for_status()
            data = rr.json()
            geo = (data.get("result") or {}).get("geographies") or {}
            res = extract_state_and_cd(geo)
            if res:
               return res

            
            raise CivicLookupError("No geocoding match for that ZIP.")

        # Full address flow → onelineaddress → geographies
        params = {
            "address": address,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "all",
            "format": "json",
        }
        r = await client.get(CENSUS_URL, params=params)
        r.raise_for_status()
        data = r.json()

        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            # fallback to Census 2020 benchmark
            params["benchmark"] = "Public_AR_Census2020"
            r2 = await client.get(CENSUS_URL, params=params)
            r2.raise_for_status()
            data = r2.json()
            matches = (data.get("result") or {}).get("addressMatches") or []
            if not matches:
                raise CivicLookupError("No geocoding match for that address.")

        geo = matches[0].get("geographies") or {}
        res = extract_state_and_cd(geo)
        if res:
            return res
        raise CivicLookupError("Could not extract state/district for that address.")


# Optional helper used by /ask when the user types a ZIP only
async def address_from_zip(zip_code: str) -> str:
    """Validate ZIP format; we pass the ZIP through to the geocoder."""
    if not (zip_code.isdigit() and len(zip_code) == 5):
        raise CivicLookupError(f"Invalid ZIP code format: {zip_code}")
    return zip_code


async def get_federal_officials(address: str) -> List[Official]:
    """Resolve an address/ZIP to current US Senators + House Rep (cached)."""
    now = time.time()
    cached = _CACHE.get(address)
    if cached and (now - cached[0] < _CACHE_TTL):
        return cached[1]

    state, district = await _census_geocode(address)
    legislators = await _load_legislators()

    senators: List[Official] = []
    representative: Optional[Official] = None

    for person in legislators:
        terms = person.get("terms") or []
        term = terms[-1] if terms else None
        if not term:
            continue
        try:
            end = dt.date.fromisoformat(term.get("end", "1900-01-01"))
            if end < dt.date.today():
                continue
        except Exception:
            # pydantic or format hiccup; ignore and continue
            pass

        if term.get("type") == "sen" and term.get("state") == state:
            senators.append(_to_official(person, term))
        elif term.get("type") == "rep" and term.get("state") == state:
            if int(term.get("district", 0)) == int(district):
                representative = _to_official(person, term)

    senators = senators[:2]
    results = senators + ([representative] if representative else [])
    if not results:
        raise CivicLookupError("No current federal officials found for that district.")

    _CACHE[address] = (now, results)
    return results

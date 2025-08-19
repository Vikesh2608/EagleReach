# backend/providers/free_civic.py
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel


# ----------------------------
# Free, keyless data sources
# ----------------------------
CENSUS_ONE_LINE_URL = (
    "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
)
CENSUS_COORDS_URL = (
    "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
)
ZIPPO_URL = "https://api.zippopotam.us/us/{zip}"
LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json"
)

# ----------------------------
# Tiny in-memory cache
# ----------------------------
_CACHE: Dict[str, Tuple[float, List["Official"]]] = {}
_CACHE_TTL = 60 * 60  # 1 hour


# ----------------------------
# Models & errors
# ----------------------------
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


# ----------------------------
# Helpers
# ----------------------------
async def _load_legislators() -> List[Dict[str, Any]]:
    """Fetch the current legislators JSON (free & static)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(LEGISLATORS_URL)
        r.raise_for_status()
        return r.json()


def _to_official(person: Dict[str, Any], term: Dict[str, Any]) -> Official:
    """Convert a legislators-current person+term into our Official model."""
    name = person.get("name", {})
    full = (
        name.get("official_full")
        or " ".join(x for x in [name.get("first"), name.get("middle"), name.get("last")] if x).strip()
        or "Unknown"
    )
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


def _extract_state_and_cd(geographies: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """
    Census 'geographies' is a dict of arrays keyed by labels like:
      - "States"
      - "118th Congressional Districts"
      - "115th Congressional Districts"
    We:
      1) pull state from 'States' (STUSAB),
      2) find *any* key containing 'Congressional District',
      3) parse the BASENAME into a district number (or 0 for at-large).
    """
    states = geographies.get("States") or []
    if not states:
        return None
    state = states[0].get("STUSAB")
    if not state:
        return None

    cd_key = next((k for k in geographies.keys() if "Congressional District" in k), None)
    if not cd_key:
        # No CD listed (at-large, or shape not present)
        return state, 0

    cd_items = geographies.get(cd_key) or []
    if not cd_items:
        return state, 0

    basename = (cd_items[0].get("BASENAME") or "").strip()
    # Common at-large strings look like "At Large"
    if basename.lower().startswith("at"):
        return state, 0

    digits = "".join(ch for ch in basename if ch.isdigit())
    district = int(digits or "0")
    return state, district


async def _geocode_zip(zip_code: str) -> Tuple[str, int]:
    """
    Resolve a ZIP to (state_abbr, congressional_district) by:
      1) Getting lat/lon from Zippopotam.us
      2) Reverse geocoding with Census 'coordinates' endpoint.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        zr = await client.get(ZIPPO_URL.format(zip=zip_code))
        if zr.status_code != 200:
            raise CivicLookupError(f"ZIP code {zip_code} not found.")
        z = zr.json()
        places = z.get("places") or []
        if not places:
            raise CivicLookupError(f"No place found for ZIP {zip_code}.")
        p = places[0]
        lat = p.get("latitude")
        lng = p.get("longitude")
        if not lat or not lng:
            raise CivicLookupError(f"Coordinates unavailable for ZIP {zip_code}.")

        params = {
            "x": float(lng),  # longitude
            "y": float(lat),  # latitude
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "all",
            "format": "json",
        }
        rr = await client.get(CENSUS_COORDS_URL, params=params)
        rr.raise_for_status()
        data = rr.json()
        geog = (data.get("result") or {}).get("geographies") or {}
        res = _extract_state_and_cd(geog)
        if res:
            return res

        # One more try with the "2020" benchmark which sometimes works better
        params["benchmark"] = "Public_AR_Census2020"
        rr2 = await client.get(CENSUS_COORDS_URL, params=params)
        rr2.raise_for_status()
        data2 = rr2.json()
        geog2 = (data2.get("result") or {}).get("geographies") or {}
        res2 = _extract_state_and_cd(geog2)
        if res2:
            return res2

        raise CivicLookupError("Census reverse geocoding failed for that ZIP.")


async def _geocode_address(address: str) -> Tuple[str, int]:
    """
    Resolve a full street address to (state_abbr, congressional_district)
    using the Census 'onelineaddress' endpoint, with a fallback benchmark.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        params = {
            "address": address,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "all",
            "format": "json",
        }
        r = await client.get(CENSUS_ONE_LINE_URL, params=params)
        r.raise_for_status()
        data = r.json()
        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            # retry with 2020 benchmark
            params["benchmark"] = "Public_AR_Census2020"
            r2 = await client.get(CENSUS_ONE_LINE_URL, params=params)
            r2.raise_for_status()
            data2 = r2.json()
            matches = (data2.get("result") or {}).get("addressMatches") or []
            if not matches:
                raise CivicLookupError("No geocoding match for that address.")

        geog = matches[0].get("geographies") or {}
        res = _extract_state_and_cd(geog)
        if res:
            return res
        raise CivicLookupError("Could not extract state/district for that address.")


# --------------------------------------------------
# Public helpers used by the /ask route
# --------------------------------------------------
async def address_from_zip(zip_code: str) -> str:
    """Validate that a user-provided ZIP looks like a ZIP; pass through as-is."""
    if not (zip_code.isdigit() and len(zip_code) == 5):
        raise CivicLookupError(f"Invalid ZIP code format: {zip_code}")
    return zip_code


async def get_federal_officials(address: str) -> List[Official]:
    """
    Main entry point for the free civic provider:
      - If a 5-digit ZIP is provided, ZIP→lat/lon→Census geographies
      - Else treat as a street address with Census 'onelineaddress'
      - Map (state,district) to Senators + House Rep using congress-legislators
    """
    now = time.time()
    cached = _CACHE.get(address)
    if cached and (now - cached[0] < _CACHE_TTL):
        return cached[1]

    # 1) Geocode → (state, district)
    if address.isdigit() and len(address) == 5:
        state, district = await _geocode_zip(address)
    else:
        state, district = await _geocode_address(address)

    # 2) Load legislators + filter by (state, district)
    legislators = await _load_legislators()
    senators: List[Official] = []
    representative: Optional[Official] = None

    for person in legislators:
        terms = person.get("terms") or []
        term = terms[-1] if terms else None
        if not term:
            continue

        # Ensure current
        try:
            end = dt.date.fromisoformat(term.get("end", "1900-01-01"))
            if end < dt.date.today():
                continue
        except Exception:
            # ignore bad dates
            pass

        ttype = term.get("type")
        tstate = term.get("state")
        if ttype == "sen" and tstate == state:
            senators.append(_to_official(person, term))
        elif ttype == "rep" and tstate == state:
            td = int(term.get("district", 0))
            if int(district) == td:
                representative = _to_official(person, term)

    senators = senators[:2]
    results = senators + ([representative] if representative else [])
    if not results:
        raise CivicLookupError("No current federal officials found for that district.")

    _CACHE[address] = (now, results)
    return results

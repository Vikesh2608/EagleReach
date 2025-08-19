# backend/providers/free_civic.py
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel

# -------------------------------
# Config / endpoints
# -------------------------------
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"

# Prefer OpenStates if configured
USE_OPENSTATES = (os.getenv("USE_OPENSTATES") or "").lower() == "true"
OPENSTATES_API_KEY = os.getenv("OPENSTATES_API_KEY")
OPENSTATES_BASE = "https://v3.openstates.org"

# -------------------------------
# Tiny in-memory cache (address -> officials)
# -------------------------------
_CACHE: Dict[str, Tuple[float, List["Official"]]] = {}
_CACHE_TTL = 3600  # 1 hour

# -------------------------------
# Data model
# -------------------------------
class Official(BaseModel):
    level: str              # 'federal' | 'state' | 'local' (we'll use 'federal' or 'state')
    office: str             # 'US Senator' | 'US Representative' | OpenStates current_role title
    name: str
    party: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    phones: List[str] = []
    urls: List[str] = []
    photo_url: Optional[str] = None
    ids: Dict[str, Any] = {}

class CivicLookupError(RuntimeError):
    pass

# -------------------------------
# Helpers — DATA LOADERS
# -------------------------------
async def _load_legislators() -> List[Dict[str, Any]]:
    """Load the current federal legislators JSON (no-auth)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(LEGISLATORS_URL)
        r.raise_for_status()
        return r.json()

# -------------------------------
# Normalization helpers
# -------------------------------
def _to_official_congress(person: Dict[str, Any], term: Dict[str, Any]) -> Official:
    """Normalize Congress-Legislators record -> Official"""
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

def _to_official_openstates(item: Dict[str, Any]) -> Official:
    """
    Normalize an OpenStates people.geo item -> Official.
    Example fields we use:
      - item["current_role"]["title"] (e.g. 'Senator', 'Representative')
      - item["jurisdiction"]["classification"] ('country' => federal)
      - item["jurisdiction"]["name"] ('United States', 'Illinois', etc.)
      - item["given_name"] + item["family_name"] (fallback to item["name"])
      - item["party"] (string)
      - item["openstates_url"], item["image"], emails in current_role? (not always)
    """
    # name
    given = (item.get("given_name") or "").strip()
    family = (item.get("family_name") or "").strip()
    name = (given + " " + family).strip() or (item.get("name") or "").strip() or "Unknown"

    # level / state
    j = item.get("jurisdiction") or {}
    classification = (j.get("classification") or "").lower()  # 'country', 'state', 'local'
    if classification == "country":
        level = "federal"
    elif classification == "state":
        level = "state"
    else:
        level = classification or "state"

    state = j.get("name") if classification == "state" else None

    # office
    current_role = item.get("current_role") or {}
    office_title = current_role.get("title") or "Representative"

    # contacts
    urls: List[str] = []
    if item.get("openstates_url"):
        urls.append(item["openstates_url"])

    # Some records include 'email' on current role or top-level; not guaranteed:
    maybe_email = item.get("email") or current_role.get("email")
    if maybe_email:
        urls.append(f"mailto:{maybe_email}")

    phones: List[str] = []
    # OpenStates does not consistently expose phone; leave empty by default

    return Official(
        level=level,
        office=office_title if level != "federal" else f"US {office_title}",
        name=name,
        party=item.get("party"),
        state=state,
        district=None,  # OpenStates people.geo does not always include a congressional district number
        phones=phones,
        urls=urls,
        photo_url=item.get("image"),
        ids={"openstates_id": item.get("id")},
    )

# -------------------------------
# Geocoding helpers
# -------------------------------
async def _zip_to_latlon(zipcode: str) -> Tuple[float, float, Optional[str]]:
    """
    Return (lat, lon, state_abbr) for a US ZIP using Zippopotam.us (no-auth).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        zr = await client.get(f"https://api.zippopotam.us/us/{zipcode}")
        if zr.status_code != 200:
            raise CivicLookupError(f"ZIP code {zipcode} not found.")
        data = zr.json()
        places = data.get("places") or []
        if not places:
            raise CivicLookupError(f"No place found for ZIP {zipcode}.")
        p = places[0]
        lat = float(p["latitude"])
        lon = float(p["longitude"])
        state_abbr = (p.get("state abbreviation") or "").strip() or None
        return lat, lon, state_abbr

async def _census_state_and_cd_from_address(address: str) -> Tuple[str, int]:
    """
    Return (state_abbr, district_num) for a full address using Census geocoder.
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

# -------------------------------
# Free federal path (Census + Congress-Legislators)
# -------------------------------
async def _free_federal_officials(address: str) -> List[Official]:
    """
    Resolve to US Senators + House Rep using free data sources only.
    Supports both ZIP-only (handled earlier) and full addresses.
    """
    # If user passed ZIP, we use ZIP -> (lat,lon) -> Census reverse -> state/cd
    if address.isdigit() and len(address) == 5:
        lat, lon, _ = await _zip_to_latlon(address)
        async with httpx.AsyncClient(timeout=20) as client:
            rev_params = {
                "x": float(lon),
                "y": float(lat),
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

            res = extract_state_and_cd(geo)
            if not res:
                raise CivicLookupError("No geocoding match for that ZIP.")
            state, district = res
    else:
        state, district = await _census_state_and_cd_from_address(address)

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
            pass

        if term.get("type") == "sen" and term.get("state") == state:
            senators.append(_to_official_congress(person, term))
        elif term.get("type") == "rep" and term.get("state") == state:
            if int(term.get("district", 0)) == int(district):
                representative = _to_official_congress(person, term)

    senators = senators[:2]
    results = senators + ([representative] if representative else [])
    if not results:
        raise CivicLookupError("No current federal officials found for that district.")
    return results

# -------------------------------
# OpenStates path (if configured)
# -------------------------------
async def _openstates_officials(address: str) -> List[Official]:
    """
    Use OpenStates people.geo with lat/lon (from Zippopotam) for ZIPs,
    or attempt to geocode full addresses with Census to get a state fallback,
    but primary is people.geo(lat,lng).
    """
    if not OPENSTATES_API_KEY:
        raise CivicLookupError("OPENSTATES_API_KEY missing")

    async with httpx.AsyncClient(timeout=20) as client:
        # Get a lat/lon if ZIP, otherwise try Census address geocode to coords
        if address.isdigit() and len(address) == 5:
            lat, lon, _ = await _zip_to_latlon(address)
        else:
            # For full addresses: Census onelineaddress -> coordinates
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
                raise CivicLookupError("No geocoding match for that address.")
            coords = (matches[0].get("coordinates") or {})
            if not coords:
                raise CivicLookupError("Coordinates unavailable for that address.")
            lon = coords.get("x")
            lat = coords.get("y")
            if lat is None or lon is None:
                raise CivicLookupError("Coordinates unavailable for that address.")

        url = f"{OPENSTATES_BASE}/people.geo"
        headers = {"X-API-KEY": OPENSTATES_API_KEY}
        params = {"lat": float(lat), "lng": float(lon), "per_page": 10}
        rr = await client.get(url, headers=headers, params=params)
        rr.raise_for_status()
        payload = rr.json()
        results = payload.get("results") or []

        officials: List[Official] = []
        for item in results:
            try:
                officials.append(_to_official_openstates(item))
            except Exception:
                # don't fail the whole call because one record is odd
                continue

        if not officials:
            raise CivicLookupError("No officials returned by OpenStates for that location.")
        return officials

# -------------------------------
# Public entry points
# -------------------------------
async def address_from_zip(zip_code: str) -> str:
    """Validate ZIP format; we pass the ZIP straight to the geocoder(s)."""
    if not (zip_code.isdigit() and len(zip_code) == 5):
        raise CivicLookupError(f"Invalid ZIP code format: {zip_code}")
    return zip_code

async def get_federal_officials(address: str) -> List[Official]:
    """
    Main provider entry. If OpenStates is enabled (USE_OPENSTATES=true and key present),
    we use OpenStates; otherwise we use the free federal-only path.
    (We still cache by the raw address string.)
    """
    now = time.time()
    cached = _CACHE.get(address)
    if cached and (now - cached[0] < _CACHE_TTL):
        return cached[1]

    if USE_OPENSTATES and OPENSTATES_API_KEY:
        try:
            officials = await _openstates_officials(address)
        except Exception:
            # Soft-fallback to the free federal path if OpenStates has an issue
            officials = await _free_federal_officials(address)
    else:
        officials = await _free_federal_officials(address)

    _CACHE[address] = (now, officials)
    return officials

"""
Stay Finder search engine: an Airbnb search agent powered by a local Ollama model.

The desktop app (StayFinder.py) drives this. To run a search without the UI, edit CRITERIA
below, then run:
    python StayFinderSearch.py

The model decides where to search, calls Airbnb through tool functions, weighs your
free-text preferences, and picks the best listings. The result is written as Markdown to the
reports folder (ranked picks with pros/cons and photo links), ready to turn into a slide deck.
Hard filters (price, rating, capacity, required amenities, ...) are enforced in Python,
and every fact in the report (names, prices, links, photos) comes from Airbnb data, not the model.

Requires: pip install ollama pyairbnb
Airbnb has no public API; pyairbnb uses the same endpoints as the website, so it can
break if Airbnb changes things.
"""

import html
import json
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import ollama
import pyairbnb
import requests
from pydantic import BaseModel, ValidationError
from pyairbnb import details as airbnb_details


@dataclass
class SearchCriteria:
    location: str                       # anything OpenStreetMap understands: "Asheville, NC", "Outer Banks"
    check_in: str                       # YYYY-MM-DD
    check_out: str                      # YYYY-MM-DD
    adults: int = 2
    children: int = 0
    infants: int = 0
    min_bedrooms: int = 0
    min_beds: int = 0
    min_bathrooms: int = 0
    min_price_per_night: int = 0
    max_price_per_night: int = 0        # 0 = no limit. All-in average (Airbnb's total / nights)
    place_type: str = ""                # "Entire home/apt", "Private room", or "" for any
    min_rating: float = 0.0
    min_reviews: int = 0
    required_amenities: list[str] = field(default_factory=list)  # matched against amenity + house-rule names
    superhost_only: bool = False
    guest_favorite_only: bool = False
    free_cancellation: bool = False
    preferences: str = ""               # free-text soft preferences the model weighs
    max_results: int = 8
    currency: str = "USD"


# ---------------------------------------------------------------------------
# Your filter criteria - edit this
# ---------------------------------------------------------------------------
CRITERIA = SearchCriteria(
    location="Destin, FL",
    check_in="2027-03-04",
    check_out="2027-03-07",
    adults=5,
    min_bedrooms=3,
    preferences="Near the beach - the closer the better, ideally walkable.",
    max_results=8,
)

# qwen3.8:27b (18 GB) spills a few GB past the RTX 4090 Laptop's 16 GB VRAM, so it's slower,
# but it ranks and writes better. gpt-oss:20b (THINK = "medium") is the faster all-GPU fallback.
MODEL = "qwen3.8:27b"
NUM_CTX = 32768
THINK = False               # Qwen: True/False (True takes ~1 hr here). gpt-oss: "low" / "medium" / "high"
MAX_AGENT_TURNS = 24
DETAIL_LIMIT = 40           # max listings per search to verify amenities/capacity on (~1s each, 4 at a time)
IMAGES_PER_PICK = 12        # photo URLs included for each top pick (other matches get a cover photo)
IMAGE_WIDTH = 1200          # Airbnb's CDN resizes via ?im_w=; 1200px is plenty for a 16:9 slide
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "NRSStayFinder/1.0"
REPORTS_DIR = Path(__file__).parent / "reports"

NOTABLE_AMENITY = re.compile(
    r"\b(pool|hot tub|sauna|fireplace|\w+ view|patio|balcony|backyard|grill|fire pit|ev charger|free parking|"
    r"garage|gym|washer|dryer|workspace|beach|lake|waterfront|ski|game console|arcade|ping pong|kayak|"
    r"bikes|pets allowed|air conditioning)\b",
    re.IGNORECASE,
)
BORING_AMENITY = re.compile(r"hair dryer|fireplace guards", re.IGNORECASE)
PLACEHOLDER_CAPTION = re.compile(r"Listing image \d+", re.IGNORECASE)

NIGHTS = (date.fromisoformat(CRITERIA.check_out) - date.fromisoformat(CRITERIA.check_in)).days
VERIFIED: dict[int, dict] = {}          # room_id -> summary, for every listing that passed all hard filters
SEARCH_PHOTOS: dict[int, list[str]] = {}  # room_id -> photo URLs from search results (fallback if details fail)
DETAILS_CACHE: dict[int, dict | None] = {}
GEOCODE_CACHE: dict[str, dict] = {}
SEARCH_LOG: list[str] = []

# run_search() swaps these so the UI can stream progress and offer a Stop button
LOG_HANDLER = print
STOP_REQUESTED = lambda: False


class SearchCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    LOG_HANDLER(message)


def check_stop() -> None:
    if STOP_REQUESTED():
        raise SearchCancelled("Search stopped")


def model_think_setting(model: str, think: bool):
    """gpt-oss takes a reasoning level and can't fully turn thinking off; other models take True/False."""
    if model.startswith("gpt-oss"):
        return "medium" if think else "low"
    return think


def listing_url(room_id: int) -> str:
    c = CRITERIA
    return (f"https://www.airbnb.com/rooms/{room_id}?check_in={c.check_in}&check_out={c.check_out}"
            f"&adults={c.adults}&children={c.children}&infants={c.infants}")


def strip_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def km_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def geocode(location: str) -> dict:
    if location not in GEOCODE_CACHE:
        resp = requests.get(NOMINATIM_URL, params={"q": location, "format": "json", "limit": 1},
                            headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        hits = resp.json()
        if not hits:
            raise ValueError("no match found")
        hit = hits[0]
        south, north, west, east = map(float, hit["boundingbox"])
        GEOCODE_CACHE[location] = {"name": hit["display_name"], "lat": float(hit["lat"]), "lng": float(hit["lon"]),
                                   "north": north, "east": east, "south": south, "west": west}
    return GEOCODE_CACHE[location]


def bayesian_score(listing: dict) -> float:
    # Pulls ratings with few reviews toward 4.7 so a 5.0 with 3 reviews doesn't top the list
    return (listing["rating"] * listing["reviews"] + 4.7 * 10) / (listing["reviews"] + 10)


def summarize_search_result(raw: dict, center: dict) -> dict:
    unit = raw["price"]["unit"]
    amount = unit.get("discount") or unit.get("amount") or 0
    nights_match = re.search(r"(\d+)\s+nights?", unit.get("qualifier", ""))
    if nights_match:
        total = amount
        nightly = amount / int(nights_match.group(1))
    else:
        nightly = amount
        total = amount * NIGHTS
    layout = ", ".join(line["body"] for line in (raw.get("structuredContent") or {}).get("primaryLine") or []
                       if line.get("body"))
    lat, lng = raw["coordinates"]["latitude"], raw["coordinates"]["longitud"]
    return {
        "room_id": str(raw["room_id"]),     # string: 19-digit ids lose precision as JSON numbers
        "name": raw.get("name") or raw.get("title"),
        "type": raw.get("title"),
        "layout": layout,
        "price_per_night": round(nightly),
        "total_price": round(total),
        "rating": float(raw["rating"]["value"] or 0),
        "reviews": int(raw["rating"]["reviewCount"] or 0),
        "guest_favorite": "GUEST_FAVORITE" in raw.get("badges", []),
        "free_cancellation": any(m.get("type") == "FREE_CANCELLATION_HIGHLIGHT"
                                 for m in raw.get("paymentMessages") or []),
        "km_from_search_center": round(km_between(center["lat"], center["lng"], lat, lng), 1),
    }


def search_level_rejection(listing: dict) -> str | None:
    c = CRITERIA
    if c.max_price_per_night and listing["price_per_night"] > c.max_price_per_night:
        return "over max price"
    if listing["price_per_night"] < c.min_price_per_night:
        return "under min price"
    if listing["rating"] < c.min_rating:
        return "rating too low"
    if listing["reviews"] < c.min_reviews:
        return "too few reviews"
    if c.guest_favorite_only and not listing["guest_favorite"]:
        return "not guest favorite"
    return None


def fetch_details(room_id: int) -> dict | None:
    if room_id not in DETAILS_CACHE:
        try:
            data, _, _ = airbnb_details.get(f"https://www.airbnb.com/rooms/{room_id}", "en", "")
            DETAILS_CACHE[room_id] = data
        except Exception as ex:
            log(f"     ! details failed for {room_id}: {ex}")
            DETAILS_CACHE[room_id] = None
    return DETAILS_CACHE[room_id]


def available_amenities(details: dict) -> list[str]:
    return [a["title"] for group in details.get("amenities", []) for a in group["values"] if a.get("available")]


def notable_amenities(details: dict) -> list[str]:
    return [a for a in available_amenities(details) if NOTABLE_AMENITY.search(a) and not BORING_AMENITY.search(a)]


def slide_image_url(url: str) -> str:
    return f"{url.split('?')[0]}?im_w={IMAGE_WIDTH}"


def listing_photos(room_id: int, limit: int) -> list[dict]:
    """Photos in the host's order (first is the cover), with the host's caption when it has one."""
    details = DETAILS_CACHE.get(room_id) or {}
    photos = [{"url": slide_image_url(img["url"]),
               "caption": None if PLACEHOLDER_CAPTION.fullmatch(img.get("title") or "") else img.get("title") or None}
              for img in details.get("images", []) if img.get("url")]
    if not photos:
        photos = [{"url": slide_image_url(url), "caption": None} for url in SEARCH_PHOTOS.get(room_id, [])]
    return photos[:limit]


def house_rules(details: dict) -> list[str]:
    return [v["title"] for group in details.get("house_rules", {}).get("general", []) for v in group["values"]]


def detail_level_rejection(details: dict | None) -> str | None:
    c = CRITERIA
    if details is None:
        return "details unavailable" if (c.required_amenities or c.superhost_only) else None
    if (details.get("person_capacity") or 99) < c.adults + c.children:
        return "too small for group"
    if c.place_type and details.get("room_type") and details["room_type"] != c.place_type:
        return "wrong place type"
    if c.superhost_only and not details.get("is_super_host"):
        return "not superhost"
    searchable = " | ".join(available_amenities(details) + house_rules(details))
    for amenity in c.required_amenities:
        # Whole words, so "Washer" doesn't match "Dishwasher"
        if not re.search(rf"\b{re.escape(amenity)}\b", searchable, re.IGNORECASE):
            return f"missing {amenity}"
    return None


# ---------------------------------------------------------------------------
# Tools exposed to the model (docstrings become the tool schemas)
# ---------------------------------------------------------------------------
def search_listings(location: str, radius_km: float = 0) -> str:
    """Run a real Airbnb search for an area using the user's dates, guests and hard filters.

    Only listings that pass every hard filter (price, rating, reviews, bedrooms, capacity,
    required amenities, etc.) are returned, so you don't need to re-check those.

    Args:
        location: Place to search, e.g. "Asheville, NC", "West Asheville", "Black Mountain, NC"
        radius_km: Optional. Search a box of this radius around the place's center instead of its boundary. Use for small towns, landmarks, or "near X" requests.

    Returns:
        JSON with filter statistics and the matching candidates, best-rated first.
    """
    c = CRITERIA
    radius_km = float(radius_km or 0)
    try:
        place = geocode(location)
    except Exception as ex:
        return json.dumps({"error": f"Could not find '{location}': {ex}. Try a more specific place name."})

    north, east, south, west = place["north"], place["east"], place["south"], place["west"]
    if not radius_km and max(north - south, east - west) < 0.02:
        radius_km = 5                   # a single point/landmark - give it some room
    if radius_km:
        dlat = radius_km / 111
        dlng = radius_km / (111 * math.cos(math.radians(place["lat"])))
        north, south = place["lat"] + dlat, place["lat"] - dlat
        east, west = place["lng"] + dlng, place["lng"] - dlng
    span = max(north - south, east - west)
    zoom = int(max(6, min(16, round(math.log2(360 / span)) + 1)))

    try:
        # Airbnb's price_max is the nightly base rate, which is <= our all-in nightly price,
        # so passing it only trims listings we would reject anyway
        raw = pyairbnb.search_all(
            c.check_in, c.check_out, north, east, south, west, zoom, 0, c.max_price_per_night,
            place_type=c.place_type, free_cancellation=c.free_cancellation,
            adults=c.adults, children=c.children, infants=c.infants,
            min_bedrooms=c.min_bedrooms, min_beds=c.min_beds, min_bathrooms=c.min_bathrooms,
            currency=c.currency,
        )
    except Exception as ex:
        return json.dumps({"error": f"Airbnb search failed: {ex}"})

    unique = {r["room_id"]: r for r in raw if r.get("room_id")}
    rejected = Counter()
    candidates = []
    for r in unique.values():
        listing = summarize_search_result(r, place)
        reason = search_level_rejection(listing)
        if reason:
            rejected[reason] += 1
        else:
            candidates.append(listing)

    candidates.sort(key=bayesian_score, reverse=True)
    if len(candidates) > DETAIL_LIMIT:
        rejected[f"not verified (beyond top {DETAIL_LIMIT})"] = len(candidates) - DETAIL_LIMIT
        candidates = candidates[:DETAIL_LIMIT]

    with ThreadPoolExecutor(max_workers=4) as pool:
        all_details = list(pool.map(fetch_details, [int(l["room_id"]) for l in candidates]))

    matches = []
    for listing, details in zip(candidates, all_details):
        reason = detail_level_rejection(details)
        if reason:
            rejected[reason] += 1
            continue
        if details:
            listing["superhost"] = bool(details.get("is_super_host"))
            listing["sleeps"] = details.get("person_capacity")
            listing["notable_amenities"] = notable_amenities(details)
        VERIFIED[int(listing["room_id"])] = listing
        SEARCH_PHOTOS[int(listing["room_id"])] = [img["url"] for img in unique[int(listing["room_id"])]["images"]
                                                  if img.get("url")]
        matches.append(listing)

    SEARCH_LOG.append(f"{location}{f' (radius {radius_km:g} km)' if radius_km else ''}: "
                      f"{len(unique)} listings found, {len(matches)} passed filters")
    log(f"     {len(unique)} found, {len(matches)} passed. Rejected: {dict(rejected)}")
    return json.dumps({
        "searched_area": place["name"],
        "listings_found": len(unique),
        "rejected_by_reason": dict(rejected),
        "matches": matches,
    })


def get_listing_details(room_id: str) -> str:
    """Get full details for one listing: every amenity, house rules, location notes, rating breakdown, host.

    Args:
        room_id: The room_id string of a listing returned by search_listings, copied exactly

    Returns:
        JSON with the listing's details.
    """
    room_id = int(room_id)
    if room_id not in VERIFIED:
        return json.dumps({"error": f"{room_id} is not a room_id from search_listings. Copy the id string exactly."})
    details = fetch_details(room_id)
    if details is None:
        return json.dumps({"error": f"Could not load details for {room_id}. Skip it or rely on the search data."})
    return json.dumps({
        **VERIFIED[room_id],
        "title": details.get("title"),
        "room_type": details.get("room_type"),
        "sleeps": details.get("person_capacity"),
        "superhost": details.get("is_super_host"),
        "guest_favorite": details.get("is_guest_favorite"),
        "host": details.get("host", {}).get("name"),
        "rating_breakdown": details.get("rating"),
        "highlights": [h.get("title") for h in details.get("highlights", [])],
        "amenities": available_amenities(details),
        "house_rules": house_rules(details),
        "location_notes": [f"{d.get('title')}: {strip_html(d.get('content'))[:400]}"
                           for d in details.get("location_descriptions", [])],
        "description": strip_html(details.get("description"))[:800],
    })


TOOLS = {f.__name__: f for f in (search_listings, get_listing_details)}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
class ModelPick(BaseModel):
    room_id: str
    headline: str               # slide title
    why_it_fits: str
    pros: list[str]
    cons: list[str]


class ModelReport(BaseModel):
    summary: str
    picks: list[ModelPick]
    notes: list[str]


def system_prompt() -> str:
    c = CRITERIA
    return f"""You are an Airbnb search assistant. Find the best listings for the user's trip using the tools.

Tools:
- search_listings: runs a real Airbnb search. All hard filters are already enforced by the tool.
- get_listing_details: full amenities, house rules and location notes for one listing.

Process:
1. Call search_listings for the user's location. If fewer than {c.max_results} matches come back, try nearby
   neighborhoods/towns or a radius_km search (at most 4 searches total).
2. Shortlist about {c.max_results + 2} candidates from the matches, then call get_listing_details on each one
   to check them against the user's preferences before ranking.
3. When you have finished researching, reply with just the word READY. You will then be asked for the
   final picks in a structured format.

Rules:
- Only recommend listings returned by search_listings. Never invent listings, prices or amenities.
- Only claim an amenity (fireplace, view, hot tub, private vs shared beach...) if that listing's amenities
  say so. A fire pit is not a fireplace; "Shared beach access" is not private.
- Prices are in {c.currency}. price_per_night is Airbnb's total price divided by {NIGHTS} nights.
- km_from_search_center is the distance from the center of the place that was searched."""


def final_report_prompt() -> str:
    c = CRITERIA
    return f"""Now give your final picks as JSON. This will be turned into a slide deck, one slide per pick.

- summary: 2-3 sentences for a title slide: what was searched and the overall takeaway.
- picks: up to {c.max_results} listings, best first. Each has:
  - room_id: copied exactly from search_listings
  - headline: a catchy slide title of at most 8 words (not the listing's name)
  - why_it_fits: 2-3 sentences on why it suits the user's preferences
  - pros: 2-4 bullets, each at most 10 words
  - cons: 1-3 bullets, each at most 10 words
- notes: 2-5 short bullets on trade-offs and anything to double-check before booking.

Don't include names, prices, ratings or links; those are added automatically from Airbnb's data.
Every claim must match the listing data you retrieved.

Schema: {json.dumps(ModelReport.model_json_schema())}"""


def user_prompt() -> str:
    c = CRITERIA
    hard = {k: v for k, v in asdict(c).items() if k not in ("preferences", "max_results") and v not in (0, "", [], False)}
    return (f"Find Airbnb stays for this trip ({NIGHTS} nights).\n\n"
            f"Hard filters (already enforced by search_listings):\n{json.dumps(hard, indent=2)}\n\n"
            f"Preferences to weigh when ranking:\n{c.preferences or '(none)'}")


def chat(messages: list, use_tools: bool = True, schema: dict | None = None):
    return ollama.chat(model=MODEL, messages=messages, tools=list(TOOLS.values()) if use_tools else None,
                       format=schema, think=THINK, options={"num_ctx": NUM_CTX}).message


def research(messages: list) -> None:
    """Let the model search and inspect listings until it says it's done (or runs out of turns)."""
    tools_used = Counter()
    nudged = False
    for turn in range(1, MAX_AGENT_TURNS + 1):
        check_stop()
        log(f"[turn {turn}] thinking...")
        msg = chat(messages)
        messages.append(msg)
        if not msg.tool_calls:
            # Models like to skip straight to answering; make them look at the listings first
            if VERIFIED and tools_used["get_listing_details"] < 3 and not nudged:
                nudged = True
                log("  (asking the model to check listing details before finishing)")
                messages.append({"role": "user", "content": "Before finishing, call get_listing_details "
                                 "on each of your shortlisted candidates to verify they fit the preferences."})
                continue
            return
        for call in msg.tool_calls:
            name, args = call.function.name, call.function.arguments or {}
            tools_used[name] += 1
            log(f"  -> {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            try:
                result = TOOLS[name](**args) if name in TOOLS else json.dumps({"error": f"Unknown tool {name}"})
            except Exception as ex:
                result = json.dumps({"error": f"{type(ex).__name__}: {ex}"})
            messages.append({"role": "tool", "content": result, "tool_name": name})
    log("[max turns reached]")


def run_agent() -> ModelReport:
    messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user_prompt()}]
    research(messages)

    log("[final] writing picks as JSON...")
    messages.append({"role": "user", "content": final_report_prompt()})
    for attempt in range(2):
        content = chat(messages, use_tools=False, schema=ModelReport.model_json_schema()).content or ""
        try:
            return ModelReport.model_validate_json(content)
        except ValidationError as ex:
            log(f"  ! invalid JSON from model (attempt {attempt + 1}): {ex.errors()[0]['msg']}")
    return ModelReport(summary="The model did not return valid picks; see other_matches.", picks=[], notes=[])


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def listing_record(listing: dict, photo_limit: int) -> dict:
    room_id = int(listing["room_id"])
    photos = listing_photos(room_id, photo_limit)
    return {
        "room_id": listing["room_id"],
        "url": listing_url(room_id),
        "name": listing["name"],
        "property_type": listing["type"],
        "layout": listing["layout"],
        "sleeps": listing.get("sleeps"),
        "price_per_night": listing["price_per_night"],
        "total_price": listing["total_price"],
        "rating": listing["rating"],
        "reviews": listing["reviews"],
        "superhost": listing.get("superhost", False),
        "guest_favorite": listing["guest_favorite"],
        "free_cancellation": listing["free_cancellation"],
        "km_from_search_center": listing["km_from_search_center"],
        "notable_amenities": listing.get("notable_amenities", []),
        "cover_image": photos[0]["url"] if photos else None,
        "images": photos,
    }


def build_output(report: ModelReport) -> dict:
    c = CRITERIA
    warnings = []
    picks, picked_ids = [], set()
    for pick in report.picks:
        room_id = int(pick.room_id) if pick.room_id.isdigit() else None
        if room_id not in VERIFIED:
            warnings.append(f"Dropped pick {pick.room_id!r}: not a listing returned by a verified search.")
            continue
        if room_id in picked_ids:
            continue
        picked_ids.add(room_id)
        picks.append({
            "rank": len(picks) + 1,
            **listing_record(VERIFIED[room_id], IMAGES_PER_PICK),
            "headline": pick.headline,
            "why_it_fits": pick.why_it_fits,
            "pros": pick.pros,
            "cons": pick.cons,
        })

    others = sorted((l for rid, l in VERIFIED.items() if rid not in picked_ids), key=bayesian_score, reverse=True)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "trip": {
            "location": c.location,
            "check_in": c.check_in,
            "check_out": c.check_out,
            "nights": NIGHTS,
            "adults": c.adults,
            "children": c.children,
            "infants": c.infants,
            "currency": c.currency,
        },
        "criteria": asdict(c),
        "summary": report.summary,
        "picks": picks,
        "notes": report.notes,
        "other_matches": [listing_record(l, 1) for l in others],
        "search_log": SEARCH_LOG,
        "warnings": warnings,
    }


def md_text(value) -> str:
    """One-line text that won't break a table row or link label."""
    return re.sub(r"\s+", " ", str(value)).replace("|", "/").replace("[", "(").replace("]", ")").strip()


def listing_facts(listing: dict) -> str:
    facts = [f"${listing['price_per_night']:,}/night", f"${listing['total_price']:,} total",
             f"{listing['rating']:.2f}★ ({listing['reviews']} reviews)" if listing["reviews"] else "New listing",
             listing["layout"]]
    if listing["sleeps"]:
        facts.append(f"sleeps {listing['sleeps']}")
    return " · ".join(f for f in facts if f)


def render_markdown(output: dict) -> str:
    trip = output["trip"]
    lines = [
        f"# Airbnb picks: {trip['location']}",
        "",
        f"_{trip['check_in']} → {trip['check_out']} ({trip['nights']} nights) · {trip['adults']} adults"
        f"{f', {trip['children']} children' if trip['children'] else ''}"
        f" · generated {output['generated_at'].replace('T', ' ')} by `{output['model']}`_",
        "",
        output["summary"],
        "",
        "## Criteria",
        "",
        "| Filter | Value |",
        "|---|---|",
    ]
    for key, value in output["criteria"].items():
        if value not in (0, "", [], False):
            lines.append(f"| {key} | {md_text(', '.join(value) if isinstance(value, list) else value)} |")

    lines += ["", "## Top picks", ""]
    if not output["picks"]:
        lines += ["_No picks: see the other matches below._", ""]
    for pick in output["picks"]:
        badges = [b for b, on in (("Superhost", pick["superhost"]), ("Guest favorite", pick["guest_favorite"]),
                                  ("Free cancellation", pick["free_cancellation"])) if on]
        lines += [
            f"### {pick['rank']}. {pick['headline']}",
            "",
            f"**[{md_text(pick['name'])}]({pick['url']})** · {pick['property_type']}",
            "",
            listing_facts(pick) + (f"  \n{' · '.join(badges)}" if badges else ""),
            "",
        ]
        if pick["cover_image"]:
            lines += [f"![{md_text(pick['images'][0]['caption'] or pick['name'])}]({pick['cover_image']})", ""]
        lines += [pick["why_it_fits"], "", "**Pros**", ""] + [f"- {p}" for p in pick["pros"]]
        lines += ["", "**Cons**", ""] + [f"- {c}" for c in pick["cons"]]
        if pick["notable_amenities"]:
            lines += ["", f"**Notable amenities:** {', '.join(pick['notable_amenities'])}"]
        if len(pick["images"]) > 1:
            lines += ["", "**Photos**", ""]
            lines += [f"{i}. [{md_text(img['caption'] or f'Photo {i}')}]({img['url']})"
                      for i, img in enumerate(pick["images"], 1)]
        lines += [""]

    if output["notes"]:
        lines += ["## Notes", ""] + [f"- {n}" for n in output["notes"]] + [""]
    if output["warnings"]:
        lines += [f"> **Warning:** {w}" for w in output["warnings"]] + [""]

    lines += [
        f"## Other matches ({len(output['other_matches'])})",
        "",
        "| # | Listing | Layout | $/night | Total | Rating | Free cancel | Notable amenities | Photo |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, l in enumerate(output["other_matches"], 1):
        lines.append(
            f"| {i} | [{md_text(l['name'])}]({l['url']}) ({md_text(l['property_type'])}) | {md_text(l['layout'])} "
            f"| ${l['price_per_night']:,} | ${l['total_price']:,} "
            f"| {f'{l['rating']:.2f} ({l['reviews']})' if l['reviews'] else 'New'} "
            f"| {'Yes' if l['free_cancellation'] else ''} | {md_text(', '.join(l['notable_amenities']))} "
            f"| {f'[photo]({l['cover_image']})' if l['cover_image'] else ''} |"
        )

    lines += ["", "## Search log", ""] + [f"- {entry}" for entry in output["search_log"] or ["(no searches were run)"]]
    return "\n".join(lines) + "\n"


def run_search(criteria: SearchCriteria, model: str | None = None, think=None,
               on_log=print, should_stop=lambda: False) -> tuple[Path, dict]:
    """Run a full search and write the Markdown report. Returns (report path, report data).

    Raises SearchCancelled if should_stop() returns True partway through.
    """
    global CRITERIA, NIGHTS, MODEL, THINK, LOG_HANDLER, STOP_REQUESTED
    CRITERIA, LOG_HANDLER, STOP_REQUESTED = criteria, on_log, should_stop
    MODEL = model or MODEL
    THINK = THINK if think is None else think
    NIGHTS = (date.fromisoformat(criteria.check_out) - date.fromisoformat(criteria.check_in)).days
    if NIGHTS <= 0:
        raise ValueError("check_out must be after check_in")
    VERIFIED.clear()                    # results are per search; the details/geocode caches can carry over
    SEARCH_PHOTOS.clear()
    SEARCH_LOG.clear()

    log(f"Searching Airbnb for {criteria.location}, {criteria.check_in} → {criteria.check_out} using {MODEL}\n")
    report = run_agent()
    check_stop()
    output = build_output(report)
    slug = re.sub(r"[^a-z0-9]+", "-", criteria.location.lower()).strip("-")
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"airbnb_{slug}_{criteria.check_in}.md"
    out_path.write_text(render_markdown(output), encoding="utf-8")
    log(f"\n{len(output['picks'])} picks, {len(output['other_matches'])} other matches. Written to {out_path}")
    for warning in output["warnings"]:
        log(f"  ! {warning}")
    return out_path, output


def main():
    try:
        run_search(CRITERIA)
    except ValueError as ex:
        raise SystemExit(str(ex))


if __name__ == "__main__":
    main()

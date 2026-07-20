"""
Metro-area location matching — Indeed-style.

A naive substring filter ("los angeles") misses most of a metro's jobs:
LA-area postings say "Santa Monica", "Burbank", "Culver City", "Irvine"...
(measured live: 126 LA-metro jobs vs 37 naive matches). When the user's
location query names a known metro, expand it to the metro's city list;
anything else falls back to plain substring, unchanged.

Used by BOTH the browse endpoint (GET /jobs/) and auto-apply's saved-filter
check, so what you see is what the bot targets.
"""

# City/keyword lists are matched as case-insensitive substrings of the job's
# location field. Keep entries lowercase and specific enough not to false-
# match (e.g. never a bare "la" — it's inside "Atlanta" and "Portland").
METRO_AREAS: dict[str, list[str]] = {
    "los angeles": [
        "los angeles", "santa monica", "culver city", "burbank", "glendale",
        "pasadena", "long beach", "el segundo", "playa vista", "venice, ca",
        "west hollywood", "hollywood", "torrance", "manhattan beach",
        "irvine", "santa ana", "anaheim", "costa mesa", "newport beach",
        "orange county", "greater los angeles",
    ],
    "sf bay area": [
        "san francisco", "oakland", "san jose", "palo alto", "mountain view",
        "sunnyvale", "menlo park", "redwood city", "berkeley", "cupertino",
        "santa clara", "south san francisco", "bay area", "fremont",
    ],
    "new york": [
        "new york", "brooklyn", "manhattan", "queens", "jersey city",
        "hoboken", "nyc",
    ],
    "seattle": [
        "seattle", "bellevue", "redmond", "kirkland",
    ],
}

# Short/common aliases → canonical metro key.
_ALIASES = {
    "la": "los angeles",
    "l.a.": "los angeles",
    "los angeles metro": "los angeles",
    "greater los angeles": "los angeles",
    "orange county": "los angeles",
    "bay area": "sf bay area",
    "san francisco": "sf bay area",
    "sf": "sf bay area",
    "silicon valley": "sf bay area",
    "nyc": "new york",
    "new york city": "new york",
    "ny": "new york",
}


def expand_location(value: str) -> list[str]:
    """
    Return the list of location substrings a query should match.
    Known metro (or alias) → its city list; anything else → [value] as-is.
    """
    norm = (value or "").strip().lower()
    if not norm:
        return []
    key = _ALIASES.get(norm, norm)
    return list(METRO_AREAS.get(key, [norm]))

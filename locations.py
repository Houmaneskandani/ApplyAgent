"""
Metro-area location matching — Indeed-style. Plus job_region(): is a
posting in the US?

A naive substring filter ("los angeles") misses most of a metro's jobs:
LA-area postings say "Santa Monica", "Burbank", "Culver City", "Irvine"...
(measured live: 126 LA-metro jobs vs 37 naive matches). When the user's
location query names a known metro, expand it to the metro's city list;
anything else falls back to plain substring, unchanged.

Used by BOTH the browse endpoint (GET /jobs/) and auto-apply's saved-filter
check, so what you see is what the bot targets.
"""
import re

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


# ── Country classification: is a posting in the US? ─────────────────────────
# Company boards (Greenhouse/Lever) list EVERY office worldwide, so a US-based
# user's list filled with Bengaluru / Dublin / Toronto / London / Sydney roles
# (measured 2026-11-19: ~2,500 of 9,700 listed jobs). job_region() classifies
# a posting's free-text location ONCE — stored in jobs.region at insert and by
# backfill — so SQL filters on the stored value instead of re-implementing
# this heuristic (no drift between the list, auto-apply and the scorer).
#
#   'us'      — names a US place, or a multi-location posting that includes one
#   'non_us'  — names only places outside the US
#   'unknown' — "Remote", "Hybrid", "" ... can't tell. Never hidden.

def _words(items) -> str:
    return "|".join(sorted((re.escape(i) for i in items), key=len, reverse=True))


_US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
]
_US_CITIES = [
    "san francisco", "oakland", "berkeley", "san jose", "santa clara",
    "sunnyvale", "mountain view", "palo alto", "menlo park", "redwood city",
    "cupertino", "fremont", "bay area", "silicon valley", "los angeles",
    "santa monica", "culver city", "el segundo", "playa vista", "burbank",
    "pasadena", "glendale", "long beach", "irvine", "costa mesa",
    "newport beach", "santa ana", "anaheim", "orange county", "san diego",
    "sacramento", "seattle", "bellevue", "redmond", "kirkland", "portland",
    "nyc", "brooklyn", "manhattan", "jersey city", "hoboken", "boston",
    "somerville", "chicago", "austin", "dallas", "houston", "san antonio",
    "plano", "frisco", "denver", "boulder", "atlanta", "miami", "tampa",
    "orlando", "raleigh", "charlotte", "nashville", "pittsburgh",
    "philadelphia", "baltimore", "reston", "herndon", "mclean", "bethesda",
    "phoenix", "scottsdale", "tempe", "chandler", "salt lake city", "lehi",
    "provo", "boise", "las vegas", "minneapolis", "detroit", "ann arbor",
    "columbus", "cincinnati", "cleveland", "st. louis", "saint louis",
    "kansas city", "indianapolis", "milwaukee", "omaha", "des moines",
    "new orleans", "honolulu", "albuquerque", "hawthorne", "starbase",
    "mcgregor", "cape canaveral", "stamford", "new haven", "providence",
]
# Phrases that mean "the US is (one of) the places": checked FIRST, so a
# multi-office posting that includes a US office stays visible.
_US_STRONG_RE = re.compile(
    r"(?<![a-z])(?:united states|u\.s\.a\.?|u\.s\.?|usa|us|north america|americas|"
    + _words(_US_STATE_NAMES) + "|" + _words(_US_CITIES) + r")(?![a-z])",
    re.I,
)

_NON_US_AREAS = [
    "australia", "new zealand", "canada", "mexico", "brazil", "argentina",
    "chile", "colombia", "peru", "uruguay", "costa rica", "united kingdom",
    "uk", "u.k.", "great britain", "england", "scotland", "wales",
    "northern ireland", "ireland", "germany", "deutschland", "france", "spain",
    "portugal", "italy", "netherlands", "holland", "belgium", "luxembourg",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland",
    "iceland", "poland", "czech republic", "czechia", "slovakia", "hungary",
    "romania", "bulgaria", "greece", "turkey", "türkiye", "ukraine", "serbia",
    "croatia", "slovenia", "estonia", "latvia", "lithuania", "israel", "india",
    "pakistan", "bangladesh", "sri lanka", "nepal", "singapore", "japan",
    "china", "hong kong", "taiwan", "south korea", "korea", "philippines",
    "vietnam", "thailand", "malaysia", "indonesia", "south africa", "nigeria",
    "kenya", "egypt", "morocco", "ghana", "uae", "united arab emirates",
    "saudi arabia", "qatar", "emea", "apac", "latam", "europe", "eu", "asia",
    "africa", "middle east", "oceania", "anz", "dach", "nordics", "benelux",
    "latin america", "south america", "central america", "british columbia",
    "alberta", "quebec", "québec", "nova scotia", "manitoba", "saskatchewan",
    "new brunswick", "newfoundland", "karnataka", "maharashtra", "telangana",
    "tamil nadu", "haryana", "uttar pradesh", "kerala", "gujarat",
    "west bengal", "voivodeship", "masovian", "bavaria", "greater london",
    "kuwait", "ksa", "eea", "cyprus", "malta",
]
_NON_US_AREA_RE = re.compile(r"(?<![a-z])(?:" + _words(_NON_US_AREAS) + r")(?![a-z])", re.I)
# "Vancouver, BC" / "Toronto, ON, CA" / "Remote, CAN": Canadian codes (uppercase).
_CA_PROVINCE_RE = re.compile(r"(?:^|[\s,(/;|])(?:ON|BC|AB|QC|NS|MB|SK|NB|NL|PE|CAN)(?=$|[\s,)/;|.])")

# Foreign cities. A few share a name with a US town — those count as US only
# when the matching state follows ("Dublin, CA", "Vancouver, WA", "Vienna, VA").
_US_NAMESAKES = {
    "dublin": ("CA", "OH", "GA", "VA", "TX", "PA"), "vancouver": ("WA",),
    "vienna": ("VA",), "paris": ("TX", "KY", "TN", "IL"), "london": ("KY", "OH"),
    "athens": ("GA", "OH", "TN", "AL", "TX"), "rome": ("GA", "NY"),
    "melbourne": ("FL",), "berlin": ("NH", "CT", "MD", "NJ", "PA", "WI", "VT"),
    "manchester": ("NH", "CT", "VT"), "birmingham": ("AL", "MI"),
    "bristol": ("CT", "TN", "VA", "RI", "PA"), "oxford": ("MS", "OH", "AL", "CT"),
    "waterloo": ("IA",), "halifax": ("VA", "PA", "MA", "NC"),
    "wellington": ("FL",), "amsterdam": ("NY",), "geneva": ("IL", "NY"),
}
_NON_US_CITIES = list(_US_NAMESAKES) + [
    "toronto", "montreal", "montréal", "ottawa", "calgary", "edmonton",
    "kitchener", "winnipeg", "edinburgh", "glasgow", "leeds", "belfast", "cork",
    "galway", "lyon", "munich", "münchen", "frankfurt", "hamburg", "cologne",
    "köln", "stuttgart", "düsseldorf", "rotterdam", "utrecht", "eindhoven",
    "the hague", "brussels", "antwerp", "zurich", "zürich", "lausanne", "basel",
    "stockholm", "gothenburg", "malmö", "malmo", "oslo", "copenhagen", "aarhus",
    "helsinki", "reykjavik", "reykjavík", "warsaw", "krakow", "kraków",
    "wroclaw", "wrocław", "gdansk", "gdańsk", "poznan", "poznań", "prague",
    "brno", "budapest", "bucharest", "cluj", "sofia", "belgrade", "zagreb",
    "lisbon", "porto", "madrid", "barcelona", "valencia", "milan", "milano",
    "turin", "tel aviv", "haifa", "jerusalem", "herzliya", "bangalore",
    "bengaluru", "hyderabad", "pune", "mumbai", "new delhi", "delhi", "gurgaon",
    "gurugram", "noida", "chennai", "kolkata", "ahmedabad", "tokyo", "osaka",
    "seoul", "beijing", "shanghai", "shenzhen", "hangzhou", "taipei", "manila",
    "jakarta", "kuala lumpur", "bangkok", "ho chi minh", "hanoi", "sydney",
    "brisbane", "adelaide", "canberra", "auckland", "sao paulo", "são paulo",
    "rio de janeiro", "buenos aires", "bogota", "bogotá", "medellin",
    "medellín", "mexico city", "guadalajara", "monterrey", "santiago",
    "montevideo", "cape town", "johannesburg", "lagos", "nairobi", "cairo",
    "dubai", "abu dhabi", "riyadh", "doha", "istanbul", "karachi", "lahore",
    "dhaka", "colombo",
]
_NON_US_CITY_RE = re.compile(r"(?<![a-z])(?:" + _words(_NON_US_CITIES) + r")(?![a-z])", re.I)
_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    "MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|"
    "WI|WY|DC"
)
# A bare state code as its own token: "Remote, TX", "SF, CA", "NY".
_STATE_CODE_RE = re.compile(r"(?:^|[\s,(/;|\-–])(?:" + _STATE_CODES + r")(?=$|[\s,)/;|.\-–])")


def job_region(location: str | None) -> str:
    """Classify a posting's location: 'us', 'non_us' or 'unknown'."""
    raw = (location or "").strip()
    if not raw:
        return "unknown"
    if _US_STRONG_RE.search(raw):
        return "us"
    if _NON_US_AREA_RE.search(raw) or _CA_PROVINCE_RE.search(raw):
        return "non_us"
    hits = list(_NON_US_CITY_RE.finditer(raw))
    if hits:
        for m in hits:
            states = _US_NAMESAKES.get(m.group(0).lower(), ())
            after = re.match(r"\s*,?\s*(" + "|".join(states) + r")(?![A-Za-z])", raw[m.end():]) if states else None
            if not after:
                return "non_us"
        return "us"
    if _STATE_CODE_RE.search(raw):
        return "us"
    return "unknown"


_US_COUNTRY_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a.", "u.s", "america"}


def us_only_for(prefs: dict | None) -> bool:
    """Should this user see / be scored on / auto-apply to US jobs only?

    Explicit prefs.us_only wins; otherwise ON when the profile's country is
    the United States. Empty country = no restriction (never hide a
    non-US user's whole feed)."""
    p = prefs or {}
    if isinstance(p.get("us_only"), bool):
        return p["us_only"]
    return (p.get("country") or "").strip().lower() in _US_COUNTRY_NAMES

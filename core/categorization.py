"""CRA T2125 auto-categorization from vendor name and receipt text."""
from __future__ import annotations

from config import CRA_LINES

# (keywords, cra_line) — checked in order, first match wins
_RULES: list[tuple[list[str], str]] = [
    # Telephone & Utilities
    (["rogers", "bell canada", "telus", "shaw", "cogeco", "videotron",
      "fido", "koodo", "virgin mobile", "freedom mobile",
      "hydro one", "toronto hydro", "enbridge", "union gas",
      "electricity", "hydro", "internet service", "wireless"], "9220"),

    # Meals & Entertainment
    (["restaurant", "pizza", "burger", "sushi", "ramen", "pho",
      "tim hortons", "timhortons", "mcdonald", "mcdonalds", "starbucks",
      "second cup", "coffee", "cafe", "bar ", "pub", "grill", "diner",
      "bistro", "brasserie", "kitchen", "eatery", "food court",
      "skip the dishes", "skipthedishes", "doordash", "ubereats",
      "uber eats"], "8523"),

    # Travel
    (["air canada", "westjet", "porter airlines", "air transat",
      "via rail", "viarail", "greyhound", "megabus",
      "marriott", "hilton", "hyatt", "sheraton", "holiday inn",
      "best western", "airbnb", "hotel", "motel",
      "enterprise rent", "hertz", "budget car"], "9200"),

    # Motor vehicle
    (["esso", "petro canada", "petrocanada", "shell", "sunoco",
      "pioneer gas", "costco gas", "gas station", "petrol",
      "canadian tire auto", "jiffy lube", "midas", "mr. lube",
      "oil change", "tire"], "9281"),

    # Advertising
    (["facebook ads", "google ads", "meta ads", "linkedin ads",
      "twitter ads", "instagram ads", "advertising", "ad spend",
      "mailchimp", "constant contact", "hootsuite"], "8521"),

    # Office Expenses
    (["staples", "bureau en gros", "office depot", "amazon",
      "amzn", "best buy", "bestbuy", "microsoft 365", "adobe",
      "google workspace", "dropbox", "zoom", "slack",
      "ups store", "fedex office", "printing"], "8810"),

    # Supplies (default catch-all for big-box stores)
    (["costco", "walmart", "home depot", "canadian tire",
      "rona", "home hardware", "ikea", "dollarama",
      "dollar tree", "loblaws", "sobeys", "metro grocery",
      "real canadian superstore"], "8811"),

    # Business taxes & memberships
    (["cpa canada", "cra payment", "government of canada",
      "ontario business", "chamber of commerce",
      "professional association", "membership dues",
      "city of toronto", "municipality fee"], "8600"),

    # Insurance
    (["intact insurance", "aviva", "td insurance", "rbc insurance",
      "desjardins insurance", "wawanesa", "insurance premium"], "8690"),

    # Bank charges
    (["bank fee", "service charge", "atm fee", "wire transfer fee",
      "rbc", "td bank", "scotiabank", "cibc", "bmo",
      "tangerine", "simplii", "interac fee"], "8710"),
]

_CAPITAL_ASSET_KEYWORDS = {
    "laptop", "macbook", "imac", "mac mini", "mac pro",
    "computer", "desktop", "server", "iphone", "ipad",
    "surface pro", "thinkpad", "dell xps", "printer",
    "scanner", "monitor", "display",
}


def auto_categorize(vendor: str, text: str = "") -> tuple[str | None, str | None]:
    """Return (cra_line, cra_description) using keyword matching."""
    haystack = (vendor + " " + text).lower()
    for keywords, line in _RULES:
        if any(kw in haystack for kw in keywords):
            return line, CRA_LINES[line]
    return None, None


def get_cra_options() -> list[tuple[str, str]]:
    """Return list of (line_code, description) tuples for UI dropdowns."""
    return list(CRA_LINES.items())


def is_capital_asset(text: str) -> bool:
    """True if receipt text suggests a capital asset purchase."""
    lower = text.lower()
    return any(kw in lower for kw in _CAPITAL_ASSET_KEYWORDS)

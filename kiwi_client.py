import os, requests, functools, datetime as dt
from cachetools import TTLCache, cached
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("TEQUILA_ENDPOINT", "https://api.tequila.kiwi.com")
HEADERS = {"apikey": os.getenv("TEQUILA_API_KEY")}

loc_cache = TTLCache(maxsize=5_000, ttl=1_800)


@cached(loc_cache)
def resolve_location(term: str, limit: int = 5):
    resp = requests.get(
        f"{BASE}/locations/query",
        headers=HEADERS,
        params={
            "term": term,
            "locale": "en-US",
            "location_types": "airport,city",
            "limit": limit,
            "active_only": True,
        },
        timeout=8,
    )
    resp.raise_for_status()
    return resp.json()["locations"]


def search_multimodal(fly_from, fly_to, date_from, date_to, adults=1, currency="GBP"):
    def convert_date_format(date_str):
        try:
            date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj.strftime("%d/%m/%Y")
        except ValueError:
            return date_str

    params = {
        "fly_from": fly_from,
        "fly_to": fly_to,
        "date_from": convert_date_format(date_from),
        "date_to": convert_date_format(date_to),
        "adults": adults,
        "curr": currency,
        "limit": 10,
    }
    r = requests.get(f"{BASE}/v2/search", headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]

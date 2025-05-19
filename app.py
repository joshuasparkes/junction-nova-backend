import os
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()


CONTENT_API_BASE = "https://content-api.sandbox.junction.dev"
API_KEY = os.getenv("CONTENT_API_KEY", "jk_live_01j8r3grxbeve8ta0h1t5qbrvx")

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "dbname": os.getenv("DB_NAME"),
}

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SEC", 5))
MAX_POLL_ATTEMPTS = int(os.getenv("MAX_POLL_ATTEMPTS", 12))
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


@app.route("/places", methods=["GET"])
def get_places():
    """
    Proxy the Content API's places lookup.
    Query params:
      ?iata=ABC
    Returns JSON: { items: [ ... ] }
    """
    iata = request.args.get("iata", "").strip().upper()
    # Mirror the client-side length checks:
    if len(iata) != 3:
        return jsonify({"items": []})

    # Build and call the external API
    url = f"{CONTENT_API_BASE}/places?filter[iata][eq]={iata}&page[limit]=5"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers)
    if not resp.ok:
        # On error, return empty items
        return jsonify({"items": []}), resp.status_code

    data = resp.json()
    return jsonify({"items": data.get("items", [])})


@app.route("/flight-search", methods=["POST"])
def flight_search():
    """
    Expects JSON:
      {
        "originId": "...",
        "destinationId": "...",
        "departureAfter": "YYYY-MM-DDTHH:MM:SSZ",
        "passengerAges": [ { "dateOfBirth": "YYYY-MM-DD" }, … ]
      }
    """
    body = request.get_json()
    if not body:
        abort(400, "Invalid JSON")

    create_url = f"{CONTENT_API_BASE}/flight-searches"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    app.logger.info(f"Requesting flight search creation with body: {body}")
    resp = requests.post(create_url, json=body, headers=headers)
    app.logger.info(f"Flight search creation response status: {resp.status_code}")
    if not resp.ok:
        app.logger.error(f"Search creation failed: {resp.text}")
        abort(resp.status_code, f"Search creation failed: {resp.text}")

    loc = resp.headers.get("Location", "")
    app.logger.info(f"Location header from flight search creation: {loc}")

    match = None
    if loc:
        parts = loc.strip("/").split("/")
        # Expecting .../flight-searches/{flight_search_id}/offers OR .../flight-searches/{flight_search_id}
        if len(parts) >= 2 and parts[-1] == "offers" and parts[-3] == "flight-searches":
            # Format: .../flight-searches/THE_ID/offers
            potential_match = parts[-2]
            if potential_match.startswith("flight_search_"):
                match = potential_match
        elif len(parts) >= 1 and parts[-2] == "flight-searches":
            # Format: .../flight-searches/THE_ID
            potential_match = parts[-1]
            if potential_match.startswith("flight_search_"):
                match = potential_match

    app.logger.info(f"Extracted flight_search_id (match): {match}")
    if not match:
        app.logger.error(
            f"Could not reliably extract flight_search_id from Location: {loc}. Extracted: {match}"
        )
        abort(
            500,
            f"Could not reliably extract flight_search_id from Location header. Received: {loc}",
        )

    offers = poll_for_offers(match)
    return jsonify(offers or {"items": []})


def poll_for_offers(search_id):
    """Poll the content API until offers are ready or we hit max attempts."""
    app.logger.info(f"Polling for offers with search_id: {search_id}")
    url = f"{CONTENT_API_BASE}/flight-searches/{search_id}/offers"
    app.logger.info(f"Polling URL: {url}")
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    attempts = 0
    while True:
        attempts += 1
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            text = resp.text.strip()
            # sometimes empty / non-JSON
            if text.startswith("{") and text.endswith("}"):
                return resp.json()
            return None
        elif resp.status_code == 202 and attempts < MAX_POLL_ATTEMPTS:
            time.sleep(POLL_INTERVAL)
            continue
        else:
            resp.raise_for_status()


@app.route("/bookings", methods=["POST"])
def create_booking():
    """
    Expects booking payload exactly like your RN code builds:
      {
        "offerId": "...",
        "passengers": [ { … }, … ]
      }
    """
    payload = request.get_json()
    if not payload:
        abort(400, "Invalid JSON")

    url = f"{CONTENT_API_BASE}/bookings"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers)
    text = resp.text
    if not resp.ok:
        abort(resp.status_code, f"Booking failed: {text}")
    return jsonify(resp.json())


@app.route("/db-data", methods=["GET"])
def db_data():
    """
    Example of reading from your Postgres.
    Adjust the table name and query as needed.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM public.bookings LIMIT 20;")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify(rows)
    except psycopg2.OperationalError as e:
        return jsonify({"error": f"Database connection failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)

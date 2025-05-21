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
API_KEY = os.getenv("CONTENT_API_KEY")

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
    Expects booking payload.
    """
    payload = request.get_json()
    if not payload:
        app.logger.error("Create booking: Invalid JSON received.")
        abort(400, "Invalid JSON")

    app.logger.info(f"Received booking payload: {payload}")

    url = f"{CONTENT_API_BASE}/bookings"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    app.logger.info(f"Proxying booking creation to URL: {url}")
    resp = requests.post(url, json=payload, headers=headers)

    # It's good to log the raw response text, especially for errors
    response_text = resp.text
    app.logger.info(
        f"Booking creation response status: {resp.status_code}, Text: {response_text}"
    )

    if not resp.ok:
        app.logger.error(f"Booking failed: {resp.status_code} - {response_text}")
        # Return the actual error message from the Content API if possible
        try:
            error_json = resp.json()
            abort(resp.status_code, description=error_json)
        except ValueError:  # If response is not JSON
            abort(
                resp.status_code,
                description=f"Booking creation failed: {response_text}",
            )

    try:
        response_json = resp.json()
        return jsonify(response_json)
    except (
        ValueError
    ):  # If successful response is not JSON (should not happen for this API)
        app.logger.error(
            f"Booking successful but response was not JSON: {response_text}"
        )
        return (
            jsonify(
                {
                    "message": "Booking successful, but response format was unexpected.",
                    "raw_response": response_text,
                }
            ),
            resp.status_code,
        )


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


def poll_for_train_offers(train_search_id):
    """Poll the Content API for train offers until ready or max attempts."""
    app.logger.info(f"Polling for train offers with train_search_id: {train_search_id}")
    url = f"{CONTENT_API_BASE}/train-searches/{train_search_id}/offers"
    app.logger.info(f"Train offers polling URL: {url}")
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
        app.logger.info(f"Train offers polling attempt {attempts}/{MAX_POLL_ATTEMPTS}")
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            text = resp.text.strip()
            if text.startswith("{") and text.endswith("}"):
                app.logger.info("Train offers received (200 OK).")
                return resp.json()
            app.logger.info(
                "Train offers received (200 OK) but response was empty/non-JSON."
            )
            return None  # Or an empty structure like {"items": []}
        elif resp.status_code == 202 and attempts < MAX_POLL_ATTEMPTS:
            app.logger.info(
                f"Train offers not ready yet (202 Accepted). Waiting {POLL_INTERVAL}s."
            )
            time.sleep(POLL_INTERVAL)
            continue
        else:
            app.logger.error(
                f"Failed to get train offers. Status: {resp.status_code}, Text: {resp.text}"
            )
            resp.raise_for_status()  # Will raise an HTTPError


@app.route("/train-station-suggestions", methods=["GET"])
def train_station_suggestions():
    query = request.args.get("name", "").strip()
    if len(query) < 3:  # Or whatever minimum length makes sense
        return jsonify({"items": []})

    url = f"{CONTENT_API_BASE}/places?filter[name][like]={query}&filter[type][eq]=railway-station&page[limit]=5"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
    }
    app.logger.info(f"Fetching train station suggestions for '{query}' from URL: {url}")
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({"items": data.get("items", [])})
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error fetching train station suggestions: {e}")
        return jsonify({"items": [], "error": str(e)}), 500


@app.route("/train-search", methods=["POST"])
def train_search():
    body = request.get_json()
    if not body:
        app.logger.error("Train search: Invalid JSON received.")
        abort(400, "Invalid JSON")

    create_url = f"{CONTENT_API_BASE}/train-searches"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    app.logger.info(
        f"Requesting train search creation with body: {body} to URL: {create_url}"
    )

    try:
        resp = requests.post(create_url, json=body, headers=headers)
        app.logger.info(
            f"Train search creation response status: {resp.status_code}, Text: {resp.text[:200]}"
        )  # Log snippet of text
        resp.raise_for_status()  # Check for HTTP errors for search creation

        loc = resp.headers.get("Location", "")
        app.logger.info(f"Location header from train search creation: {loc}")

        train_search_id = None
        if loc:
            parts = loc.strip("/").split("/")
            # Expecting .../train-searches/{train_search_id}/offers (new) OR .../train-searches/{train_search_id} (old)
            if (
                len(parts) >= 2
                and parts[-1] == "offers"
                and parts[-3] == "train-searches"
            ):
                potential_match = parts[-2]
                if potential_match.startswith("train_search_"):
                    train_search_id = potential_match
            elif len(parts) >= 1 and parts[-2] == "train-searches":
                potential_match = parts[-1]
                if potential_match.startswith("train_search_"):
                    train_search_id = potential_match

        app.logger.info(f"Extracted train_search_id: {train_search_id}")
        if not train_search_id:
            app.logger.error(
                f"Could not reliably extract train_search_id from Location: {loc}"
            )
            abort(
                500,
                f"Could not reliably extract train_search_id from Location header. Received: {loc}",
            )

        offers = poll_for_train_offers(train_search_id)
        return jsonify(offers or {"items": []})

    except requests.exceptions.HTTPError as e:
        # Log the error and response if available
        error_message = f"Train search Content API error: {e}"
        if e.response is not None:
            error_message += f" - Response: {e.response.text}"
            try:
                # Try to return JSON error from upstream if possible
                return jsonify(e.response.json()), e.response.status_code
            except ValueError:
                # Fallback if error response is not JSON
                return (
                    jsonify(
                        {"error": "Upstream API error", "details": e.response.text}
                    ),
                    e.response.status_code,
                )
        app.logger.error(error_message)
        abort(500, description=error_message)  # Fallback generic error
    except Exception as e:
        app.logger.error(f"Unexpected error in /train-search: {str(e)}")
        abort(500, str(e))


@app.route("/cancellations/request", methods=["POST"])
def request_cancellation():
    payload = request.get_json()
    if not payload or "bookingId" not in payload:
        app.logger.error("Request cancellation: Invalid JSON or missing bookingId.")
        abort(400, "Invalid JSON or missing bookingId")

    booking_id = payload["bookingId"]
    url = f"{CONTENT_API_BASE}/cancellations/request"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    app.logger.info(
        f"Proxying booking cancellation request. Target URL: {url}, Booking ID in payload: {payload.get('bookingId')}"
    )

    try:
        app.logger.info(
            f"Attempting to POST to Content API for booking {booking_id} at {url}"
        )
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        app.logger.info(
            f"Content API POST call completed for booking {booking_id}. Status: {resp.status_code if resp else 'No response object'}"
        )

        response_text = resp.text
        app.logger.debug(
            f"Booking cancellation request response status: {resp.status_code}, Text: {response_text[:200]}"
        )
        if not resp.ok:
            app.logger.error(
                f"Booking cancellation request failed for {booking_id}: {resp.status_code} - {response_text}"
            )
            try:
                return jsonify(resp.json()), resp.status_code
            except ValueError:
                return (
                    jsonify(
                        {
                            "error": "Cancellation request failed",
                            "details": response_text,
                        }
                    ),
                    resp.status_code,
                )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.Timeout:
        app.logger.error(
            f"Content API call timed out for booking {booking_id} at {url}"
        )
        return (
            jsonify(
                {
                    "error": "Cancellation request timed out",
                    "details": f"Timeout after 15 seconds for {url}",
                }
            ),
            504,
        )
    except requests.exceptions.RequestException as e:
        app.logger.error(
            f"Network error during booking cancellation for {booking_id} to {url}: {e}"
        )
        return (
            jsonify(
                {
                    "error": "Network error during cancellation request",
                    "details": str(e),
                }
            ),
            503,
        )
    except Exception as e:
        app.logger.error(
            f"Unexpected error during booking cancellation for {booking_id} to {url}: {e}"
        )
        return (
            jsonify({"error": "An unexpected error occurred", "details": str(e)}),
            500,
        )


@app.route("/bookings/<path:booking_id>/confirm-cancellation", methods=["POST"])
def confirm_booking_cancellation(booking_id):
    payload = request.get_json()
    if not payload:
        app.logger.error(
            f"Confirm cancellation for booking {booking_id}: Invalid JSON."
        )
        abort(400, "Invalid JSON")

    url = f"{CONTENT_API_BASE}/bookings/{booking_id}/confirm-cancellation"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    app.logger.info(
        f"Proxying booking confirm cancellation to URL: {url} for booking ID: {booking_id}"
    )

    try:
        resp = requests.post(url, json=payload, headers=headers)
        response_text = resp.text
        app.logger.debug(
            f"Booking confirm cancellation response status: {resp.status_code}, Text: {response_text[:200]}"
        )
        if not resp.ok:
            app.logger.error(
                f"Booking confirm cancellation failed for {booking_id}: {resp.status_code} - {response_text}"
            )
            try:
                return jsonify(resp.json()), resp.status_code
            except ValueError:
                return (
                    jsonify(
                        {
                            "error": "Confirm cancellation failed",
                            "details": response_text,
                        }
                    ),
                    resp.status_code,
                )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as e:
        app.logger.error(
            f"Network error during confirm booking cancellation for {booking_id}: {e}"
        )
        return (
            jsonify(
                {
                    "error": "Network error during confirm cancellation",
                    "details": str(e),
                }
            ),
            503,
        )
    except Exception as e:
        app.logger.error(
            f"Unexpected error during confirm booking cancellation for {booking_id}: {e}"
        )
        return (
            jsonify({"error": "An unexpected error occurred", "details": str(e)}),
            500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4000, debug=True)

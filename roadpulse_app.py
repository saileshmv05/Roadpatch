"""
RoadPulse - "Google Reviews for Roads"
A civic platform prototype built with Streamlit + Google Maps + SQLite.

Run with:
    pip install -r requirements.txt
    streamlit run roadpulse_app.py

REQUIRED: a Google Maps JavaScript API key (needs a GCP project with
billing enabled - Google gives a monthly free usage credit, but the API
itself won't load without billing turned on).
    1. https://console.cloud.google.com -> create/select a project
    2. Enable "Maps JavaScript API"
    3. Create an API key (APIs & Services > Credentials)
    4. PowerShell: $env:GOOGLE_MAPS_API_KEY = "AIza..."
    Then run streamlit in the SAME terminal window.

OPTIONAL: "Sign in with Google" (local username/password accounts always
still work without this - it's an additional option, not a replacement).
    1. https://console.cloud.google.com -> APIs & Services -> OAuth consent screen
       (External is fine for a demo; add your own Google account as a test user)
    2. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID -> Web application
       Authorized redirect URI: http://localhost:8501  (must match exactly, no trailing slash)
    3. PowerShell:
       $env:GOOGLE_OAUTH_CLIENT_ID = "....apps.googleusercontent.com"
       $env:GOOGLE_OAUTH_CLIENT_SECRET = "..."
    Then run streamlit in the SAME terminal window.

Architecture note: there is no mature "streamlit-folium"-equivalent for
Google Maps, so the map is a small HAND-BUILT Streamlit component
(./gmaps_component/index.html) - plain HTML/JS, no npm/React build.
It talks to Google Maps directly and reports clicks/drawn lines back to
Python via Streamlit's component wire protocol.

Features in this version:
    - Sign up / sign in (hashed passwords, SQLite-backed). Submitting,
      upvoting, replying, and marking-fixed all require an account.
    - One-upvote-per-account per review, enforced with a DB unique
      constraint (not just client-side).
    - Named replies - every reply shows who posted it.
    - Point OR drawn-segment reviews, with click-to-select and a draw tool.
    - "Use my current location" via browser geolocation.
    - A free location SEARCH box (OpenStreetMap Nominatim, no API key -
      independent of the Google Maps switch, so no billing needed for this part).
    - Before/After Fix Proof: mark a road Fixed with a photo, flips its pin green.
    - Civic Score & badges, tied to your account (persists across sessions).
    - Municipal Repair Priority Heatmap (Google Maps visualization library).
    - SLA countdown timer: a public 30-day repair clock on severe, open issues.
    - Municipal complaint auto-fill: known real portal for Chennai, reverse-
      geocoded generic fallback everywhere else. Optional AI (Groq) button
      to polish the complaint wording - needs GROQ_API_KEY, degrades gracefully.
    - Computer Vision damage verification: an uploaded photo is checked by
      Groq vision to confirm it's actually road damage (filters out
      irrelevant/joke images) and classify severity, auto-filling the
      category/rating. Reuses GROQ_API_KEY - no separate key needed.
    - Community News Feed: real Google News RSS headlines about road issues,
      upvotable by the community (NOT Facebook scraping - see comment on
      fetch_news_rss for why that's off the table).
    - Hero-inspired red/blue/web visual theme (original CSS, no copyrighted
      artwork/logos reproduced).

NOTE on auth: this uses a simple SHA-256 hashed password stored in SQLite -
good enough for a hackathon demo, but a real deployment would want salted
hashing (bcrypt/argon2) and proper session/token management.
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
DB_FILE = "road_reviews.db"
UPLOAD_DIR = "uploads"
CHENNAI_COORDS = [13.0827, 80.2707]  # Default map center: Chennai, India
CATEGORIES = ["Pothole", "Waterlogging", "Traffic/Cracks"]
SLA_DAYS = 30

# --------------------------------------------------------------------------
# Municipal complaint routing
# --------------------------------------------------------------------------
# We only have ONE real, verified portal on file: Greater Chennai
# Corporation's PGR system. For anywhere else, we reverse-geocode the
# review's coordinates to find the local district/city name, and if it's
# not a place we recognize, we hand the citizen a pre-filled generic
# complaint plus a search link to find their own municipal office's
# portal - honest about what we do and don't actually know.
MUNICIPALITY_DIRECTORY = {
    "chennai": {
        "name": "Greater Chennai Corporation (GCC)",
        "url": "https://erp.chennaicorporation.gov.in/pgr/citizen/BeforeReg.do",
        "categories": {
            "Pothole": {"group": "Road and Footpath", "type": "Pot hole fill up / Repairs to the damaged surface"},
            "Waterlogging": {"group": "Water Stagnation", "type": "Stagnation of Water"},
            "Traffic/Cracks": {"group": "Road and Footpath", "type": "Pot hole fill up / Repairs to the damaged surface"},
        },
    },
}
GCC_DETAILS_MAX_CHARS = 400  # matches the real GCC portal's "Details of Complaint" field limit

# Generic fallback office terminology by country - "Municipal Corporation
# / Panchayat Office" means nothing in Miami, just like "City Hall / 311"
# would sound odd for rural India. Keyed by ISO 3166-1 alpha-2 country
# code (what Nominatim's reverse-geocode returns), lowercase.
COUNTRY_OFFICE_LABELS = {
    "in": "Municipal Corporation / Panchayat Office",
    "us": "City Hall / Department of Public Works (311)",
    "gb": "Local Council",
    "ca": "Municipal Office / City Hall",
    "au": "Local Council",
    "ie": "County/City Council",
    "nz": "City/District Council",
    "sg": "Municipal Services Office (OneService)",
    "za": "Municipal Office",
}
DEFAULT_OFFICE_LABEL = "Local Government / Public Works Office"

# --------------------------------------------------------------------------
# Anti-spam / anti-duplicate settings
# --------------------------------------------------------------------------
SUBMISSION_COOLDOWN_SECONDS = 120  # 2 minutes between reviews, per account - adjust to taste
DUPLICATE_RADIUS_METERS = 40  # same-category open report within this radius = "already reported"


@st.cache_data(ttl=3600)
def reverse_geocode(lat, lon):
    """
    Free, no-API-key reverse geocoding via OpenStreetMap's Nominatim -
    turns a review's coordinates into a district/city name AND a country
    code, so we know both which municipal office it falls under and what
    to CALL that office (terminology varies wildly by country). Cached
    for an hour (keyed on rounded coordinates by the caller) since
    Nominatim asks callers not to hammer it with repeat requests.
    Returns (district_name, country_code) - both None on network failure.
    """
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "addressdetails": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Hackathon-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, None
    address = data.get("address", {})
    district_name = (
        address.get("city") or address.get("town") or address.get("municipality")
        or address.get("county") or address.get("state_district") or address.get("state")
    )
    country_code = (address.get("country_code") or "").lower() or None
    return district_name, country_code


def get_municipality_info(row):
    """
    Returns (municipality_info_dict, is_known_real_portal). For an
    unrecognized district, builds a Google search link instead of
    guessing a URL we can't verify, and picks office terminology that
    actually matches the country the review is in.
    """
    district_name, country_code = reverse_geocode(round(row["lat"], 3), round(row["lon"], 3))

    if district_name:
        key = district_name.lower()
        for name_key, info in MUNICIPALITY_DIRECTORY.items():
            if name_key in key:
                return info, True

    # Unknown / unmatched district - generic fallback, no invented portal URL.
    label = district_name or "your area"
    office_label = COUNTRY_OFFICE_LABELS.get(country_code, DEFAULT_OFFICE_LABEL)
    search_query = urllib.parse.quote_plus(f"{label} {office_label} public grievance road repair complaint")
    return (
        {
            "name": f"{label} {office_label}",
            "url": f"https://www.google.com/search?q={search_query}",
            "categories": None,
        },
        False,
    )
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8501")

st.set_page_config(page_title="RoadPulse", page_icon="🕷️", layout="wide")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# THEME - hero-inspired red/blue/web palette (original CSS, no copyrighted
# artwork or logos - just color, font, and a hand-drawn web pattern).
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Bangers&display=swap');

    .stApp {
        background:
            repeating-radial-gradient(circle at 50% -10%,
                rgba(226,54,54,0.03) 0px, rgba(226,54,54,0.03) 1px,
                transparent 1px, transparent 48px),
            radial-gradient(circle at 50% -10%, #142248 0%, #0b0e1a 65%);
    }

    .hero-banner {
        background: linear-gradient(120deg, #3a0a0a 0%, #0b0e1a 75%);
        border-bottom: 3px solid #e23636;
        border-radius: 0 0 14px 14px;
        padding: 20px 26px;
        margin: -16px -16px 20px -16px;
        position: relative;
    }
    .hero-banner .eyebrow {
        font-family: monospace;
        letter-spacing: 2px;
        color: #ff9b8f;
        font-size: 12px;
        text-transform: uppercase;
    }
    .hero-banner h1 {
        margin: 4px 0 6px 0 !important;
        font-size: 2.4rem !important;
    }
    .hero-banner p {
        color: #d7dbe8;
        margin: 0;
        max-width: 640px;
    }
    .spidey-badge {
        position: absolute;
        top: 20px;
        right: 26px;
        background: #f5f3ee;
        color: #1b3d8f;
        font-family: 'Bangers', cursive;
        font-size: 13px;
        letter-spacing: 1px;
        padding: 6px 14px;
        border-radius: 6px;
        transform: rotate(3deg);
        box-shadow: 2px 2px 0 #e23636;
    }

    h1, h2, h3 {
        font-family: 'Bangers', cursive !important;
        letter-spacing: 1.5px;
        color: #e23636 !important;
        text-shadow: 2px 2px 0px #1b3d8f;
    }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0d1b3d 0%, #14142b 100%);
        border-right: 3px solid #e23636;
    }

    div.stButton > button, .stForm button, div.stLinkButton > a {
        background-color: #e23636 !important;
        color: white !important;
        border: 2px solid #1b3d8f !important;
        font-weight: 700;
        border-radius: 8px !important;
    }
    div.stButton > button:hover, div.stLinkButton > a:hover {
        background-color: #1b3d8f !important;
        border-color: #e23636 !important;
        color: white !important;
    }

    .hero-card {
        background: linear-gradient(135deg, #1b3d8f 0%, #0d1b3d 100%);
        border: 2px solid #e23636;
        border-radius: 12px;
        padding: 12px 14px;
        margin-bottom: 10px;
        color: #fafafa;
    }
    .hero-card b { color: #ff8a80; }

    .stTabs [data-baseweb="tab"] {
        font-family: 'Bangers', cursive !important;
        font-size: 16px;
        letter-spacing: 1px;
        color: #dfe6ff !important;
    }
    .stTabs [aria-selected="true"] {
        color: #ff6659 !important;
        border-bottom-color: #e23636 !important;
    }

    div[data-testid="stExpander"] {
        border: 1px solid #1b3d8f !important;
        border-radius: 10px !important;
    }

    div[data-testid="stMetricValue"] { color: #ff6659 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Registers our hand-built HTML/JS components. `path` points at the folder
# containing each index.html - no build step, Streamlit just serves it as-is.
_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmaps_component")
gmaps_component = components.declare_component("gmaps_component", path=_component_dir)

_geo_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_component")
geo_component = components.declare_component("geo_component", path=_geo_component_dir)


# --------------------------------------------------------------------------
# DATABASE LAYER
# --------------------------------------------------------------------------
def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            civic_score   INTEGER DEFAULT 0,
            created_at    TEXT    NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(users)")
    user_cols = [col[1] for col in cursor.fetchall()]
    if "email" not in user_cols:
        # Separate from `username` on purpose: a Google account's verified
        # email identifies WHO they are across logins, but the citizen
        # still picks their own display username - the two shouldn't be
        # forced to be the same thing.
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    # SQLite can't add a UNIQUE constraint via ALTER TABLE, so a partial
    # unique index does the same job for the email column (multiple NULLs
    # allowed, for local accounts with no linked Google email).
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS road_reviews (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lat           REAL    NOT NULL,
            lon           REAL    NOT NULL,
            location_name TEXT    NOT NULL,
            rating        INTEGER NOT NULL,
            category      TEXT    NOT NULL,
            description   TEXT,
            timestamp     TEXT    NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(road_reviews)")
    existing_cols = [col[1] for col in cursor.fetchall()]
    new_columns = {
        "path_coords": "TEXT",
        "status": "TEXT DEFAULT 'Open'",
        "photo_path": "TEXT",
        "fixed_photo_path": "TEXT",
        "username": "TEXT DEFAULT 'Anonymous'",
    }
    for col_name, col_type in new_columns.items():
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE road_reviews ADD COLUMN {col_name} {col_type}")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS review_upvotes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(review_id, username),
            FOREIGN KEY (review_id) REFERENCES road_reviews (id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS review_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id   INTEGER NOT NULL,
            reply_text  TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            FOREIGN KEY (review_id) REFERENCES road_reviews (id)
        )
        """
    )
    cursor.execute("PRAGMA table_info(review_replies)")
    reply_cols = [col[1] for col in cursor.fetchall()]
    if "username" not in reply_cols:
        cursor.execute("ALTER TABLE review_replies ADD COLUMN username TEXT DEFAULT 'Anonymous'")

    # --- Community News Feed tables ---
    # news_items holds articles pulled from Google News' public RSS feed
    # (a legitimate, keyless, ToS-compliant source - unlike scraping
    # Facebook, which we deliberately did NOT build; see the comment on
    # fetch_news_rss for why).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT    NOT NULL,
            link       TEXT    UNIQUE NOT NULL,
            source     TEXT,
            published  TEXT,
            fetched_at TEXT    NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(news_items)")
    news_cols = [col[1] for col in cursor.fetchall()]
    if "search_query" not in news_cols:
        # Ties each article to the search that found it, so the feed can
        # actually filter by what's currently typed in the search box -
        # without this, every fetch just piled into one shared pool and
        # the displayed list never changed no matter what you searched.
        cursor.execute("ALTER TABLE news_items ADD COLUMN search_query TEXT")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_upvotes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id   INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(news_id, username),
            FOREIGN KEY (news_id) REFERENCES news_items (id)
        )
        """
    )

    # --- One-time self-healing migration for pre-fix news rows ---
    # Older rows may have been saved with the RAW RFC-822 RSS date string
    # ("Mon, 25 Aug 2026...") from before this was fixed to parse dates
    # properly. Those don't sort correctly against the newer ISO-format
    # rows ("2026-08-25 10:00:00"), so on every startup we detect and
    # normalize any leftover raw-format rows in place - no manual DB
    # cleanup required.
    cursor.execute("SELECT id, published FROM news_items")
    for news_id, published_value in cursor.fetchall():
        if not published_value:
            continue
        try:
            # Already in our ISO format - nothing to do.
            datetime.strptime(published_value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                fixed_dt = parsedate_to_datetime(published_value)
                cursor.execute(
                    "UPDATE news_items SET published = ? WHERE id = ?",
                    (fixed_dt.strftime("%Y-%m-%d %H:%M:%S"), news_id),
                )
            except (TypeError, ValueError):
                pass  # genuinely unparseable - leave it, it'll just sort a bit oddly

    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# AUTH
# --------------------------------------------------------------------------
def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, civic_score, created_at) VALUES (?, ?, 0, ?)",
            (username, hash_password(password), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "That username is already taken."
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return row is not None and row[0] == hash_password(password)


def get_civic_score(username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT civic_score FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def add_civic_points(username, points):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET civic_score = civic_score + ? WHERE username = ?", (points, username))
    conn.commit()
    conn.close()


def build_google_auth_url():
    """Builds the URL that sends the citizen to Google's own sign-in/consent screen."""
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_google_code(code):
    """
    Exchanges the one-time authorization code Google sent back for an
    access token, then uses that token to fetch the verified email
    address. Returns (email, error_message) - exactly one is None.
    """
    token_url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    try:
        token_request = urllib.request.Request(token_url, data=payload, method="POST")
        with urllib.request.urlopen(token_request, timeout=8) as response:
            token_data = json.loads(response.read().decode("utf-8"))
        access_token = token_data["access_token"]

        userinfo_request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(userinfo_request, timeout=8) as response:
            userinfo = json.loads(response.read().decode("utf-8"))
        return userinfo.get("email"), None
    except urllib.error.HTTPError as exc:
        # Google's actual reason (invalid_client, invalid_grant, etc.) is in
        # the response body - a bare str(exc) only gives "HTTP Error 401:
        # Unauthorized" and throws that detail away, so read the body too.
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            body = "(no response body)"
        return None, f"Google sign-in failed: HTTP {exc.code} - {body}"
    except Exception as exc:  # noqa: BLE001 - surface any other OAuth failure to the sidebar
        return None, f"Google sign-in failed: {exc}"


def find_username_by_email(email):
    """Looks up an existing account already linked to this Google email, if any."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def create_google_user(username, email):
    """
    Creates a new account for a first-time Google sign-in, with a
    citizen-chosen display username (never forced to be their email).
    The stored password hash is a random throwaway value - this account
    can only ever sign in via Google, never the local username/password
    form. Returns (success, error_message).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, civic_score, created_at, email) VALUES (?, ?, 0, ?, ?)",
            (username, hash_password(secrets.token_hex(16)), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), email),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "That username is already taken - try another."
    finally:
        conn.close()


def civic_badge(score):
    """Hero-themed badge tiers for the Civic Score gamification feature."""
    if score >= 100:
        return "🕸️🦸 Amazing Spider-Citizen"
    elif score >= 50:
        return "🦸 Spectacular Web-Slinger"
    elif score >= 20:
        return "🕷️ Web-Slinger in Training"
    else:
        return "🏙️ Friendly Neighborhood Newbie"


# --------------------------------------------------------------------------
# REVIEW / UPVOTE / REPLY DATA LAYER
# --------------------------------------------------------------------------
def get_seconds_since_last_submission(username):
    """
    Returns seconds since this account's most recent review, or None if
    they've never submitted one. Backs the anti-spam cooldown - reading
    MAX(timestamp) instead of a separate counter keeps this in sync with
    the reviews table automatically, no extra bookkeeping table needed.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(timestamp) FROM road_reviews WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    last_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return (datetime.now() - last_dt).total_seconds()


def haversine_distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in meters."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000  # Earth's radius in meters
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def find_nearby_duplicate(lat, lon, category):
    """
    Checks for an existing OPEN review of the same category within
    DUPLICATE_RADIUS_METERS - this is what stops five people creating five
    separate pins for the same pothole instead of upvoting the one that's
    already there. Returns the matching row as a dict, or None.
    """
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, lat, lon, location_name FROM road_reviews WHERE category = ? AND status = 'Open'",
        conn, params=(category,),
    )
    conn.close()
    for _, row in df.iterrows():
        if haversine_distance_m(lat, lon, row["lat"], row["lon"]) <= DUPLICATE_RADIUS_METERS:
            return row.to_dict()
    return None


def insert_review(lat, lon, location_name, rating, category, description, username,
                   path_coords=None, photo_path=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO road_reviews
            (lat, lon, location_name, rating, category, description,
             timestamp, path_coords, status, photo_path, username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?)
        """,
        (
            lat, lon, location_name, rating, category, description,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path_coords, photo_path, username,
        ),
    )
    conn.commit()
    conn.close()


def fetch_all_reviews():
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT r.*, COALESCE(u.upvote_count, 0) AS upvote_count
        FROM road_reviews r
        LEFT JOIN (
            SELECT review_id, COUNT(*) AS upvote_count
            FROM review_upvotes
            GROUP BY review_id
        ) u ON r.id = u.review_id
        ORDER BY r.timestamp DESC
        """,
        conn,
    )
    conn.close()
    return df


def has_upvoted(review_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM review_upvotes WHERE review_id = ? AND username = ?", (review_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def upvote_review(review_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO review_upvotes (review_id, username, timestamp) VALUES (?, ?, ?)",
            (review_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def mark_review_fixed(review_id, new_rating, fixed_photo_path):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE road_reviews SET status = 'Fixed', rating = ?, fixed_photo_path = ? WHERE id = ?",
        (new_rating, fixed_photo_path, review_id),
    )
    conn.commit()
    conn.close()


def insert_reply(review_id, username, reply_text):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO review_replies (review_id, username, reply_text, timestamp) VALUES (?, ?, ?, ?)",
        (review_id, username, reply_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def fetch_replies(review_id):
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT * FROM review_replies WHERE review_id = ? ORDER BY timestamp ASC",
        conn, params=(review_id,),
    )
    conn.close()
    return df


# --------------------------------------------------------------------------
# COMMUNITY NEWS FEED (Google News RSS - NOT Facebook)
# --------------------------------------------------------------------------
# Your teammate's idea was to surface external reports of road problems
# for the community to corroborate. Scraping Facebook posts isn't
# something we can build - it violates Meta's terms of service regardless
# of project size, and there's no public API for arbitrary post access.
# Google News' RSS feed is the legitimate equivalent: public, keyless,
# and explicitly meant to be consumed this way.
def fetch_news_rss(query, max_items=10):
    """
    Pulls headlines from Google News' public RSS search feed. Returns a
    list of dicts, or an empty list on any network/parsing failure.
    """
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Hackathon-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            xml_bytes = response.read()
    except (urllib.error.URLError, TimeoutError):
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = item.findtext("title") or "Untitled"
        link = item.findtext("link") or ""
        raw_pub_date = item.findtext("pubDate") or ""
        source_el = item.find("source")
        source = source_el.text if source_el is not None else "Google News"

        # RSS dates are RFC-822 strings ("Mon, 25 Aug 2026 10:00:00 GMT"),
        # which do NOT sort correctly as plain text - this is the actual
        # root cause of old articles behaving unpredictably in the feed.
        # Parse to a real datetime and store it as a sortable ISO string;
        # fall back to "now" if a feed ever sends a malformed date.
        try:
            published_dt = parsedate_to_datetime(raw_pub_date)
        except (TypeError, ValueError):
            published_dt = datetime.now()
        published_iso = published_dt.strftime("%Y-%m-%d %H:%M:%S")

        if link:
            items.append({"title": title, "link": link, "source": source, "published": published_iso})
    return items


def store_news_items(items, search_query):
    """
    Inserts new articles tagged with the search that found them, skipping
    ones we've already stored (by link). If the same article link was
    already stored under a DIFFERENT earlier query, this update re-tags
    it to the current query too - so re-running a search still surfaces
    it under that search, rather than silently hiding it forever.
    """
    conn = get_connection()
    cursor = conn.cursor()
    for item in items:
        try:
            cursor.execute(
                "INSERT INTO news_items (title, link, source, published, fetched_at, search_query) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item["title"], item["link"], item["source"], item["published"],
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), search_query),
            )
        except sqlite3.IntegrityError:
            # Already have this article - just make sure it's tagged
            # under this query too, so it still shows up here.
            cursor.execute("UPDATE news_items SET search_query = ? WHERE link = ?", (search_query, item["link"]))
    conn.commit()
    conn.close()


def fetch_news_for_query(search_query, limit=200):
    """
    Fetches articles matching the CURRENT search query only - this is
    what actually makes different searches show different results,
    instead of one giant pool of everything ever fetched. Sorted purely
    by publish date; upvotes are shown as a stat but never affect
    ordering, so a popular old article can't permanently bury newer ones.
    """
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT n.*, COALESCE(u.upvote_count, 0) AS upvote_count
        FROM news_items n
        LEFT JOIN (
            SELECT news_id, COUNT(*) AS upvote_count FROM news_upvotes GROUP BY news_id
        ) u ON n.id = u.news_id
        WHERE n.search_query = ?
        ORDER BY n.published DESC
        LIMIT ?
        """,
        conn, params=(search_query, limit),
    )
    conn.close()
    return df


def has_upvoted_news(news_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM news_upvotes WHERE news_id = ? AND username = ?", (news_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def upvote_news(news_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO news_upvotes (news_id, username, timestamp) VALUES (?, ?, ?)",
            (news_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# SESSION STATE
# --------------------------------------------------------------------------
defaults = {
    "current_user": None,
    "_pending_google_email": None,
    "ai_category": None,
    "ai_rating": None,
    "ai_explanation": None,
    "pending_photo_bytes": None,
    "clicked_lat": None,
    "clicked_lon": None,
    "segment_coords": None,
    "_last_picker_raw": None,
    "_last_geo_raw": None,
    "map_center": CHENNAI_COORDS,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

# --------------------------------------------------------------------------
# GOOGLE OAUTH CALLBACK - Google redirects back to this same URL with
# ?code=... after the citizen signs in. We exchange it for their verified
# email, then either log them straight in (if this Google account has
# signed in before) or ask them to pick a display username (first time
# only - never forces the username to be their email address).
# --------------------------------------------------------------------------
query_params = st.query_params
if "code" in query_params and st.session_state.current_user is None:
    oauth_code = query_params["code"]
    verified_email, oauth_error = exchange_google_code(oauth_code)
    st.query_params.clear()
    if verified_email:
        existing_username = find_username_by_email(verified_email)
        if existing_username:
            st.session_state.current_user = existing_username
        else:
            st.session_state._pending_google_email = verified_email
    else:
        st.session_state._oauth_error = oauth_error
    st.rerun()


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
def save_photo(file_bytes, prefix):
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(file_bytes)
    return path


def safe_str(value, default=""):
    """
    Safely reads an optional text column (description, photo_path, etc.).
    pandas reads a SQL NULL as NaN (a float) rather than None or "" - and
    since NaN is truthy in Python, a plain `value or default` lets it
    through unchanged instead of falling back to default. isinstance
    check avoids that trap everywhere we read one of these columns.
    """
    return value if isinstance(value, str) else default


def safe_image(path, **kwargs):
    """
    Shows a photo if its file still exists on disk, or a friendly message
    if not - instead of a hard crash. This matters specifically on
    Streamlit Community Cloud: its local filesystem is EPHEMERAL, wiped
    on every redeploy/sleep-wake cycle, so a photo_path saved earlier can
    end up pointing at a file that no longer physically exists even
    though the database row referencing it still does.
    """
    if isinstance(path, str) and os.path.isfile(path):
        st.image(path, **kwargs)
    else:
        st.caption("📷 Photo unavailable (may have been cleared on app restart).")


SEVERITY_TO_RATING = {"Mild": 3, "Severe": 2, "Critical": 1}


def analyze_road_photo(image_bytes, mime_type="image/jpeg"):
    """
    Sends an uploaded photo to Groq to (a) verify it actually shows road
    damage and (b) classify severity and category if it does. 
    """
    try:
        from groq import Groq
    except ImportError:
        return None, "Install the 'groq' package to enable photo verification (pip install groq)."

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "Set the GROQ_API_KEY environment variable to enable photo verification."

    prompt = (
        "You are verifying a citizen-submitted photo for a civic road-damage reporting platform, "
        "to filter out irrelevant, joke, or unrelated images. Look at this photo and respond with "
        "ONLY a JSON object, no other text, no markdown fences, in exactly this shape: "
        '{"is_road_issue": true or false, '
        '"category": one of ["Pothole","Waterlogging","Traffic/Cracks"] or null if is_road_issue is false, '
        '"severity": one of ["Mild","Severe","Critical"] or null if is_road_issue is false, '
        '"looks_ai_generated": true or false - your best-effort guess at whether this image is '
        'AI-generated/synthetic rather than a real photograph (look for telltale artifacts, unnatural '
        'textures, or impossible details), '
        '"explanation": a short one-sentence reason for your verdict}.'
    )

    try:
        client = Groq(api_key=api_key)
        # Groq requires images to be passed as base64 data URIs
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        image_url = f"data:{mime_type};base64,{base64_image}"

        response = client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ],
            temperature=0.0
        )
        
        raw_text = response.choices[0].message.content.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.lower().startswith("json"):
                raw_text = raw_text[4:]
        data = json.loads(raw_text.strip())
        return data, None
    except Exception as exc: 
        return None, f"Photo analysis failed: {exc}"


def polish_complaint_with_ai(details_text, category, rating, location_name):
    """
    Sends the plain complaint text to Groq and asks for a more
    formal, persuasive rewrite suitable for an official government
    complaint form.
    """
    try:
        from groq import Groq
    except ImportError:
        return None, "Install the 'groq' package to enable AI-polished complaint wording (pip install groq)."

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "Set the GROQ_API_KEY environment variable to enable AI-polished complaint wording."

    prompt = (
        "Rewrite this citizen road-complaint description as a formal, persuasive, but factual "
        "complaint suitable for a government public-grievance portal. Keep every concrete detail "
        "(location, GPS coordinates, category). Do not invent facts that aren't in the original. "
        f"Respond with ONLY the rewritten text, under {GCC_DETAILS_MAX_CHARS} characters, no preamble, "
        "no quotation marks.\n\n"
        f"Category: {category}\nSeverity rating: {rating}/5\nLocation: {location_name}\n"
        f"Original text: {details_text}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        polished = response.choices[0].message.content.strip().strip('"')
        return polished[:GCC_DETAILS_MAX_CHARS], None
    except Exception as exc:
        return None, f"AI rewrite failed: {exc}"


def build_civic_complaint(row, municipality_info, is_known_portal):
    """
    Builds the fields a citizen would paste into their local municipal
    complaint portal: the right complaint group/type wording where we
    actually know it (Chennai/GCC), a generic-but-usable version
    everywhere else, and a details string trimmed to a safe universal
    length limit (matches GCC's real 400-char field; most portals are
    similar or more generous).
    """
    if is_known_portal and municipality_info.get("categories"):
        info = municipality_info["categories"].get(row["category"], municipality_info["categories"]["Pothole"])
        group, complaint_type = info["group"], info["type"]
    else:
        group, complaint_type = "Roads / Infrastructure", row["category"]

    title = f"Road damage - {row['location_name']}"[:80]

    description = safe_str(row["description"], "Reported via the RoadPulse citizen platform.")
    prefix = f"[{row['category']}] Near {row['location_name']} (GPS: {row['lat']:.5f}, {row['lon']:.5f}). "
    details = (prefix + description)[:GCC_DETAILS_MAX_CHARS]

    return {"group": group, "type": complaint_type, "title": title, "details": details}


def geocode_location(query):
    """Free, no-API-key location search via OpenStreetMap's Nominatim."""
    url = "[https://nominatim.openstreetmap.org/search](https://nominatim.openstreetmap.org/search)?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Hackathon-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


@st.cache_data(ttl=3600)
def get_location_name(lat, lon):
    """
    Auto-derives a human-readable location name straight from the review's
    actual coordinates via reverse geocoding, instead of trusting a free-
    text field a citizen typed - which could otherwise be filled with
    nonsense, offensive, or unrelated text with no connection to the real
    spot. Falls back to a plain coordinate string if geocoding fails.
    Cached for an hour (same pattern as the other geocoding calls) since
    Nominatim asks callers not to hammer it with repeat requests.
    """
    url = "[https://nominatim.openstreetmap.org/reverse](https://nominatim.openstreetmap.org/reverse)?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 0}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Hackathon-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return f"Near {lat:.5f}, {lon:.5f}"
    return data.get("display_name") or f"Near {lat:.5f}, {lon:.5f}"


def reviews_to_payload(df):
    """
    Converts the reviews DataFrame into plain JSON-safe dicts to hand to
    the Google Maps component (declare_component args must be JSON
    serializable - a pandas DataFrame is not, so we convert row by row).
    """
    records = []
    for _, row in df.iterrows():
        records.append({
            "lat": row["lat"],
            "lon": row["lon"],
            "location_name": row["location_name"],
            "rating": int(row["rating"]),
            "category": row["category"],
            "status": row["status"],
            "username": row["username"],
            "upvote_count": int(row["upvote_count"]),
            "description": safe_str(row["description"]),
            # pandas reads a SQL NULL as NaN (a float), and NaN is truthy in
            # Python - so a plain `if row["path_coords"]` check let NaN
            # through to json.loads() and crashed. isinstance(..., str)
            # only accepts it when there's a real JSON string to parse.
            "path_coords": json.loads(row["path_coords"]) if isinstance(row["path_coords"], str) else None,
        })
    return records


# --------------------------------------------------------------------------
# SIDEBAR - AUTH
# --------------------------------------------------------------------------
if st.session_state.current_user is None:
    st.sidebar.subheader("🔐 Sign In")

    if st.session_state.get("_oauth_error"):
        st.sidebar.error(st.session_state._oauth_error)
        st.session_state._oauth_error = None

    if st.session_state.get("_pending_google_email"):
        # First-time Google sign-in - the account doesn't exist yet, so
        # ask for a display username before creating it. This is what
        # keeps a citizen's username from being forced to be their gmail.
        pending_email = st.session_state._pending_google_email
        st.sidebar.success(f"Signed in as {pending_email} - pick a username to finish.")
        chosen_username = st.sidebar.text_input("Choose a username", key="google_username_choice")
        if st.sidebar.button("Confirm Username"):
            if not chosen_username.strip():
                st.sidebar.error("Please enter a username.")
            else:
                ok, err = create_google_user(chosen_username.strip(), pending_email)
                if ok:
                    st.session_state.current_user = chosen_username.strip()
                    st.session_state._pending_google_email = None
                    st.rerun()
                else:
                    st.sidebar.error(err)
    else:
        if GOOGLE_OAUTH_CLIENT_ID:
            st.sidebar.link_button("🔵 Sign in with Google", build_google_auth_url())
        else:
            st.sidebar.caption("Google sign-in not configured (set GOOGLE_OAUTH_CLIENT_ID/SECRET to enable it).")

        with st.sidebar.expander("Use a local account instead"):
            login_tab, signup_tab = st.tabs(["Sign In", "Sign Up"])

            with login_tab:
                login_username = st.text_input("Username", key="login_username")
                login_password = st.text_input("Password", type="password", key="login_password")
                if st.button("Sign In", key="login_btn"):
                    if verify_user(login_username.strip(), login_password):
                        st.session_state.current_user = login_username.strip()
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")

            with signup_tab:
                signup_username = st.text_input("Choose a username", key="signup_username")
                signup_password = st.text_input("Choose a password", type="password", key="signup_password")
                if st.button("Sign Up", key="signup_btn"):
                    if not signup_username.strip() or not signup_password:
                        st.error("Username and password are both required.")
                    else:
                        ok, err = create_user(signup_username.strip(), signup_password)
                        if ok:
                            st.session_state.current_user = signup_username.strip()
                            st.rerun()
                        else:
                            st.error(err)

    st.sidebar.caption("Sign in to submit, upvote, reply, or mark roads fixed. Browsing is open to everyone.")
else:
    score = get_civic_score(st.session_state.current_user)
    st.sidebar.markdown(
        f"""
        <div class="hero-card">
            🕷️ <b>{st.session_state.current_user}</b><br>
            🏅 Civic Score: <b>{score}</b> · {civic_badge(score)}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Log out"):
        st.session_state.current_user = None
        st.rerun()

st.sidebar.markdown("---")

# --------------------------------------------------------------------------
# SIDEBAR - LOCATION TOOLS (decluttered: one status line always visible,
# everything else tucked into a collapsed expander)
# --------------------------------------------------------------------------
st.sidebar.header("📍 Location")

if st.session_state.segment_coords:
    points = st.session_state.segment_coords
    centroid_lat = sum(p[0] for p in points) / len(points)
    centroid_lon = sum(p[1] for p in points) / len(points)
    st.sidebar.success(f"🧵 Segment selected ({len(points)} points)")
    st.sidebar.caption(f"📍 {get_location_name(centroid_lat, centroid_lon)}")
elif st.session_state.clicked_lat is not None:
    st.sidebar.success(
        f"📍 {get_location_name(st.session_state.clicked_lat, st.session_state.clicked_lon)}"
    )
else:
    st.sidebar.caption("Nothing selected - click the map, or use a tool below.")

with st.sidebar.expander("🕸️ Web-sling my location / search"):
    geo_result = geo_component(key="geo_button", default=None)
    if geo_result and geo_result != st.session_state._last_geo_raw:
        st.session_state._last_geo_raw = geo_result
        st.session_state.clicked_lat = geo_result["latitude"]
        st.session_state.clicked_lon = geo_result["longitude"]
        st.session_state.segment_coords = None
        st.session_state.map_center = [geo_result["latitude"], geo_result["longitude"]]

    search_query = st.text_input("Search for a location", placeholder="e.g. T Nagar, Chennai")
    if st.button("🔎 Search"):
        if search_query.strip():
            result = geocode_location(search_query.strip())
            if result:
                st.session_state.clicked_lat, st.session_state.clicked_lon = result
                st.session_state.segment_coords = None
                st.session_state.map_center = list(result)
                st.success(f"Found: {result[0]:.5f}, {result[1]:.5f}")
            else:
                st.error("Couldn't find that location - try being more specific.")
        else:
            st.warning("Type a location to search first.")

    if st.button("Clear selection"):
        st.session_state.clicked_lat = None
        st.session_state.clicked_lon = None
        st.session_state.segment_coords = None
        st.rerun()

# --------------------------------------------------------------------------
# SIDEBAR - SUBMISSION FORM
# --------------------------------------------------------------------------
st.sidebar.header("📝 Submit a Road Review")

if st.session_state.current_user is None:
    st.sidebar.info("Sign in above to submit a review.")
else:
    # Both a live camera and a file upload are offered - live capture is
    # the stronger anti-AI-image safeguard (no file picker to upload a
    # pre-existing/synthetic image from), but not everyone can safely stop
    # and shoot a photo mid-drive, so upload-from-device stays available
    # too. The Groq "looks_ai_generated" check below is the safety net
    # for the upload path - a soft signal, not a guarantee either way.
    st.sidebar.caption("📸 Optional: add a photo, then verify it with AI")
    photo_method = st.sidebar.radio(
        "Photo method", ["📷 Take a live photo", "📁 Upload from device"],
        horizontal=True, label_visibility="collapsed", key="review_photo_method",
    )
    if photo_method.startswith("📷"):
        uploaded_photo = st.sidebar.camera_input("Take a photo", key="review_photo_camera")
    else:
        uploaded_photo = st.sidebar.file_uploader(
            "Upload a road photo", type=["jpg", "jpeg", "png"], key="review_photo_uploader"
        )
    if uploaded_photo is not None:
        if st.sidebar.button("🔍 Verify & Analyze with AI"):
            photo_bytes = uploaded_photo.getvalue()
            mime_type = uploaded_photo.type or "image/jpeg"
            result, ai_error = analyze_road_photo(photo_bytes, mime_type)
            if ai_error:
                st.sidebar.warning(ai_error)
            elif not result.get("is_road_issue"):
                st.sidebar.warning(
                    f"⚠️ This doesn't look like road damage: {result.get('explanation', 'no reason given')}. "
                    "You can still submit, but double-check your photo."
                )
                st.session_state.ai_category = None
                st.session_state.ai_rating = None
                st.session_state.ai_explanation = None
            else:
                if result.get("looks_ai_generated"):
                    st.sidebar.warning(
                        "🕵️ This photo has some signs of being AI-generated rather than a real "
                        "photograph - best-effort AI guess, not a certainty, but worth a second look."
                    )
                st.session_state.ai_category = result.get("category")
                st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                st.session_state.ai_explanation = result.get("explanation")
                st.sidebar.success(
                    f"✅ Verified road damage - {result.get('severity')}. Form below is pre-filled."
                )
            st.session_state.pending_photo_bytes = photo_bytes

        if st.session_state.get("ai_explanation"):
            st.sidebar.caption(f"🕷️ AI verdict: {st.session_state.ai_explanation}")

    default_category_index = (
        CATEGORIES.index(st.session_state.get("ai_category"))
        if st.session_state.get("ai_category") in CATEGORIES else 0
    )
    default_rating = st.session_state.get("ai_rating") or 3

    with st.sidebar.form("review_form", clear_on_submit=True):
        st.caption("📍 Location is captured automatically from your map selection above - no typing needed.")
        category = st.selectbox("Category", CATEGORIES, index=default_category_index)
        rating = st.slider("Rating (1 = terrible, 5 = excellent)", 1, 5, default_rating)
        description = st.text_area("Description", placeholder="Describe the road condition...")
        submitted = st.form_submit_button("Submit Review")

        if submitted:
            has_point = st.session_state.clicked_lat is not None
            has_segment = bool(st.session_state.segment_coords)
            cooldown_elapsed = get_seconds_since_last_submission(st.session_state.current_user)
            in_cooldown = cooldown_elapsed is not None and cooldown_elapsed < SUBMISSION_COOLDOWN_SECONDS

            if not has_point and not has_segment:
                st.error("Select a point, draw a segment, use your location, or search first.")
            elif in_cooldown:
                wait_seconds = int(SUBMISSION_COOLDOWN_SECONDS - cooldown_elapsed)
                st.error(f"⏱️ Slow down, citizen! Please wait {wait_seconds}s before submitting another review.")
            else:
                if has_segment:
                    points = st.session_state.segment_coords
                    centroid_lat = sum(p[0] for p in points) / len(points)
                    centroid_lon = sum(p[1] for p in points) / len(points)
                    path_json = json.dumps(points)
                else:
                    centroid_lat = st.session_state.clicked_lat
                    centroid_lon = st.session_state.clicked_lon
                    path_json = None

                duplicate = find_nearby_duplicate(centroid_lat, centroid_lon, category)

                if duplicate:
                    st.warning(
                        f"📍 A similar '{category}' report already exists nearby "
                        f"(\"{duplicate['location_name']}\"). Please upvote that one in the "
                        "Community tab instead of creating a duplicate."
                    )
                else:
                    # Location name comes straight from the coordinates, not
                    # free text - closes off the "type something nonsensical
                    # as the road name" troll vector entirely.
                    derived_location_name = get_location_name(centroid_lat, centroid_lon)

                    pending_bytes = st.session_state.get("pending_photo_bytes")
                    photo_path = save_photo(pending_bytes, "review") if pending_bytes else None

                    insert_review(
                        lat=centroid_lat, lon=centroid_lon, location_name=derived_location_name,
                        rating=rating, category=category, description=description.strip(),
                        username=st.session_state.current_user,
                        path_coords=path_json, photo_path=photo_path,
                    )
                    add_civic_points(st.session_state.current_user, 10)
                    st.success("🕸️ Thwip! Review submitted. (+10 Civic Score)")

                    for key in ("clicked_lat", "clicked_lon", "segment_coords", "_last_picker_raw", "_last_geo_raw"):
                        st.session_state[key] = defaults[key]
                    for key in ("ai_category", "ai_rating", "ai_explanation", "pending_photo_bytes"):
                        st.session_state[key] = None
                    st.rerun()


# --------------------------------------------------------------------------
# MAIN AREA - TABS
# --------------------------------------------------------------------------
st.markdown(
    """
    <div class="hero-banner">
        <div class="eyebrow">RoadPulse // Friendly Neighborhood Edition</div>
        <h1>Your Friendly Neighborhood Road Watch</h1>
        <p>With great roads comes great responsibility. Spot it, report it,
        sling it to the right office - crowdsourced road-quality reporting, built by citizens.</p>
        <div class="spidey-badge">SPIDEY-SENSE: ACTIVE</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not GOOGLE_MAPS_API_KEY:
    st.warning(
        "No Google Maps API key found. Set the GOOGLE_MAPS_API_KEY environment variable and restart "
        "to see the map (both the picker and heatmap views). (See the setup instructions at the top of roadpulse_app.py.)"
    )

tab_map, tab_reviews, tab_news = st.tabs(["🗺️ Map", "💬 Community", "📰 The Daily Bugle"])

# ----- TAB 1: MAP (interactive picker + heatmap, toggle between them) -------
with tab_map:
    reviews_df = fetch_all_reviews()

    view_choice = st.radio(
        "View", ["📍 Map (click / draw to report)", "🔥 Heatmap (fixed vs. unfixed)"],
        horizontal=True, label_visibility="collapsed",
    )
    is_heatmap_view = view_choice.startswith("🔥")

    col_map, col_stats = st.columns([3, 1])

    with col_stats:
        st.metric("Total Reviews", len(reviews_df))
        if not reviews_df.empty:
            st.metric("Average Rating", f"{reviews_df['rating'].mean():.1f} ⭐")
            open_severe = ((reviews_df["rating"] <= 2) & (reviews_df["status"] == "Open")).sum()
            st.metric("Open Bad Roads (≤2★)", int(open_severe))
        if is_heatmap_view:
            st.caption("🔴 Red = still open/unfixed &nbsp; 🟢 Green = fixed.")
        else:
            st.caption("Use the ✏️ Draw Segment button (top-right of the map) to draw a road segment.")

    with col_map:
        if GOOGLE_MAPS_API_KEY:
            zoom_level = 13 if st.session_state.map_center == CHENNAI_COORDS else 15

            if is_heatmap_view:
                gmaps_component(
                    api_key=GOOGLE_MAPS_API_KEY,
                    center={"lat": CHENNAI_COORDS[0], "lng": CHENNAI_COORDS[1]},
                    zoom=12,
                    reviews=reviews_to_payload(reviews_df),
                    enable_picker=False,
                    show_heatmap=True,
                    key="heatmap",
                    default=None,
                )
            else:
                picker_result = gmaps_component(
                    api_key=GOOGLE_MAPS_API_KEY,
                    center={"lat": st.session_state.map_center[0], "lng": st.session_state.map_center[1]},
                    zoom=zoom_level,
                    reviews=reviews_to_payload(reviews_df),
                    enable_picker=True,
                    show_heatmap=False,
                    key="road_map",
                    default=None,
                )

                if picker_result and picker_result != st.session_state._last_picker_raw:
                    st.session_state._last_picker_raw = picker_result
                    if picker_result.get("type") == "click":
                        st.session_state.clicked_lat = picker_result["lat"]
                        st.session_state.clicked_lon = picker_result["lng"]
                        st.session_state.segment_coords = None
                    elif picker_result.get("type") == "drawing":
                        st.session_state.segment_coords = picker_result["path"]
                        st.session_state.clicked_lat = None
                        st.session_state.clicked_lon = None
        else:
            st.info("Map unavailable - add a Google Maps API key (see the warning above).")

    if not is_heatmap_view:
        st.markdown(
            "**Legend:** 🔴 Red = 1-2★ (Severe) &nbsp;&nbsp; 🟠 Orange = 3★ (Moderate) "
            "&nbsp;&nbsp; 🟢 Green = 4-5★ (Good, incl. Fixed)"
        )

# ----- TAB 2: COMMUNITY (all reviews, upvotes, replies, fixes) -------------
with tab_reviews:
    st.subheader("💬 Community")
    st.caption("Every review, with photos, an upvote button, an SLA clock, and a reply thread.")

    all_reviews = fetch_all_reviews()

    if all_reviews.empty:
        st.info("No reviews yet - sign in and submit one from the sidebar!")
    else:
        for _, row in all_reviews.iterrows():
            review_id = int(row["id"])
            is_segment = isinstance(row["path_coords"], str)  # see note in reviews_to_payload re: NaN truthiness
            location_kind = "🧵" if is_segment else "📍"
            status_pill = "🟢 FIXED" if row["status"] == "Fixed" else "🔴 OPEN"
            ticket_id = f"RP-{review_id:04d}"

            with st.expander(
                f"{ticket_id} · {location_kind} {row['location_name']} · {'⭐' * int(row['rating'])} · {status_pill}"
            ):
                st.caption(f"{row['category']} · reported by **{row['username']}**")
                st.write(safe_str(row["description"], "_No description provided._"))

                if row["status"] == "Open" and int(row["rating"]) <= 2:
                    reported_dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    days_elapsed = (datetime.now() - reported_dt).days
                    if days_elapsed > SLA_DAYS:
                        st.error(f"⛔ SLA breached - reported {days_elapsed} days ago (30-day public repair limit)")
                    else:
                        st.info(f"⏳ {SLA_DAYS - days_elapsed} day(s) remaining under the 30-day repair SLA")

                # --- File with the citizen's actual local municipal office ---
                if row["status"] == "Open":
                    with st.expander("📋 File this with your local municipal office"):
                        municipality_info, is_known_portal = get_municipality_info(row)
                        st.write(f"**Reporting to:** {municipality_info['name']}")

                        if is_known_portal:
                            st.caption(
                                "This is a real public complaints system. It requires your own mobile "
                                "number + OTP to submit, so we can't file this for you - but everything "
                                "below is pre-formatted to paste straight in."
                            )
                        else:
                            st.caption(
                                "We don't have this municipality's exact portal on file yet, so here's a "
                                "search link to find it, plus a generic complaint you can adapt to whatever "
                                "form it uses."
                            )

                        civic = build_civic_complaint(row, municipality_info, is_known_portal)
                        st.write(f"**Complaint Category:** {civic['group']}")
                        st.write(f"**Complaint Type:** {civic['type']}")
                        st.write("**Complaint Title:**")
                        st.code(civic["title"], language=None)

                        # An AI-polished version is optional and session-only - we
                        # never overwrite the DB, just what's displayed here.
                        polish_key = f"ai_polished_{review_id}"
                        if st.button("✨ Improve wording with AI", key=f"ai_polish_btn_{review_id}"):
                            polished, ai_error = polish_complaint_with_ai(
                                civic["details"], row["category"], int(row["rating"]), row["location_name"]
                            )
                            if ai_error:
                                st.warning(ai_error)
                            else:
                                st.session_state[polish_key] = polished

                        details_to_show = st.session_state.get(polish_key, civic["details"])
                        st.write(f"**Details of Complaint** ({len(details_to_show)}/{GCC_DETAILS_MAX_CHARS} chars):")
                        st.code(details_to_show, language=None)
                        if polish_key in st.session_state:
                            st.caption("✨ AI-polished wording shown above. This isn't saved - re-generate anytime.")

                        if row["photo_path"] and isinstance(row["photo_path"], str):
                            st.caption("📎 Attach the photo below when you upload it on the portal:")
                            safe_image(row["photo_path"], width=200)

                        button_label = "Open Complaint Portal ↗" if is_known_portal else "Search for Local Portal ↗"
                        st.link_button(button_label, municipality_info["url"])

                col_up, _ = st.columns([1, 5])
                with col_up:
                    if st.session_state.current_user is None:
                        st.caption(f"👍 {row['upvote_count']} · sign in to upvote")
                    elif has_upvoted(review_id, st.session_state.current_user):
                        st.caption(f"✅ {row['upvote_count']} · you upvoted this")
                    else:
                        if st.button(f"👍 {row['upvote_count']}", key=f"rev_upvote_{review_id}"):
                            if upvote_review(review_id, st.session_state.current_user):
                                add_civic_points(st.session_state.current_user, 1)
                            st.rerun()

                if row["status"] == "Open":
                    st.markdown("---")
                    st.markdown("**🛠️ Mark as Fixed**")
                    if isinstance(row["photo_path"], str):
                        safe_image(row["photo_path"], caption="Reported photo", width=250)
                    if st.session_state.current_user is None:
                        st.caption("Sign in to mark this road as fixed.")
                    else:
                        fixed_photo_method = st.radio(
                            "After-photo method", ["📷 Take a live photo", "📁 Upload from device"],
                            horizontal=True, label_visibility="collapsed", key=f"fixed_photo_method_{review_id}",
                        )
                        if fixed_photo_method.startswith("📷"):
                            fixed_photo = st.camera_input(
                                "Take an after-repair photo (optional)", key=f"fixed_photo_camera_{review_id}",
                            )
                        else:
                            fixed_photo = st.file_uploader(
                                "Upload an after-repair photo (optional)", type=["jpg", "jpeg", "png"],
                                key=f"fixed_photo_upload_{review_id}",
                            )
                        new_rating = st.slider("New rating after fix", 1, 5, 5, key=f"fixed_rating_{review_id}")
                        if st.button("✅ Confirm Fixed", key=f"confirm_fixed_{review_id}"):
                            fixed_path = save_photo(fixed_photo.getvalue(), f"fixed_{review_id}") if fixed_photo else None
                            mark_review_fixed(review_id, new_rating, fixed_path)
                            add_civic_points(st.session_state.current_user, 15)
                            st.rerun()
                else:
                    st.success("✅ This road has been marked FIXED by the community.")
                    col_before, col_after = st.columns(2)
                    with col_before:
                        if isinstance(row["photo_path"], str):
                            safe_image(row["photo_path"], caption="Before")
                    with col_after:
                        if isinstance(row["fixed_photo_path"], str):
                            safe_image(row["fixed_photo_path"], caption="After")

                st.markdown("---")
                st.markdown("**Replies:**")
                replies = fetch_replies(review_id)
                if replies.empty:
                    st.caption("No replies yet - be the first to respond.")
                else:
                    for _, reply in replies.iterrows():
                        st.markdown(f"**{reply['username']}:** {reply['reply_text']}")
                        st.caption(reply["timestamp"])

                if st.session_state.current_user is None:
                    st.caption("Sign in to reply.")
                else:
                    with st.form(f"reply_form_{review_id}", clear_on_submit=True):
                        reply_text = st.text_input("Add a reply", key=f"reply_input_{review_id}")
                        reply_submitted = st.form_submit_button("Post Reply")
                        if reply_submitted:
                            if reply_text.strip():
                                insert_reply(review_id, st.session_state.current_user, reply_text.strip())
                                add_civic_points(st.session_state.current_user, 2)
                                st.rerun()
                            else:
                                st.warning("Reply can't be empty.")

# ----- TAB 3: THE DAILY BUGLE (community news feed) ------------------------
with tab_news:
    st.markdown(
        """
        <style>
        @import url('[https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&display=swap](https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&display=swap)');
        .bugle-masthead {
            border-top: 4px solid #f5f3ee; border-bottom: 4px solid #f5f3ee;
            padding: 10px 0; margin-bottom: 14px; text-align: center;
        }
        .bugle-masthead h1 {
            font-family: 'Playfair Display', serif !important;
            font-size: 2.6rem !important; letter-spacing: 1px;
            color: #f5f3ee !important; text-shadow: none !important;
            margin: 0 !important;
        }
        .bugle-masthead .subhead {
            font-family: Georgia, serif; font-style: italic; color: #b8bccb;
            font-size: 13px; letter-spacing: 2px; text-transform: uppercase;
        }
        .bugle-article b { font-family: 'Playfair Display', serif; font-size: 1.05rem; }
        </style>
        <div class="bugle-masthead">
            <div class="subhead">Serving the neighborhood since day one</div>
            <h1>THE DAILY BUGLE</h1>
            <div class="subhead">Real news. Real roads. Real citizens.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "Real news coverage of road/infrastructure problems, pulled from Google News. "
        "Sorted newest first - upvotes show community trust, but never bump an article out of order."
    )

    news_query = st.text_input(
        "Search topic", value="Chennai road damage OR pothole", key="news_search_query"
    )
    if st.button("🔄 Fetch Latest Headlines"):
        fetched = fetch_news_rss(news_query.strip())
        if fetched:
            store_news_items(fetched, news_query.strip())
            st.session_state.news_page = 1  # jump back to page 1 on a fresh fetch
            st.success(f"Fetched {len(fetched)} articles.")
        else:
            st.warning("No articles found, or the request failed - try a different search topic.")
        st.rerun()

    if "news_page" not in st.session_state:
        st.session_state.news_page = 1
    if "_last_shown_news_query" not in st.session_state:
        st.session_state._last_shown_news_query = None
    if st.session_state._last_shown_news_query != news_query.strip():
        # The search box changed since the last render - reset to page 1
        # rather than potentially landing on an out-of-range page for a
        # topic with fewer results than the last one.
        st.session_state.news_page = 1
        st.session_state._last_shown_news_query = news_query.strip()

    PAGE_SIZE = 10
    all_news = fetch_news_for_query(news_query.strip())

    if all_news.empty:
        st.info("No headlines for this topic yet - click 'Fetch Latest Headlines' above to pull some in.")
    else:
        total_pages = max(1, (len(all_news) + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.news_page = min(st.session_state.news_page, total_pages)
        page = st.session_state.news_page

        start = (page - 1) * PAGE_SIZE
        page_items = all_news.iloc[start:start + PAGE_SIZE]

        for _, item in page_items.iterrows():
            news_id = int(item["id"])
            with st.container(border=True):
                st.markdown(f'<div class="bugle-article"><b>{item["title"]}</b></div>', unsafe_allow_html=True)
                st.caption(f"{item['source']} · {item['published']}")
                st.link_button("Read article ↗", item["link"])

                if st.session_state.current_user is None:
                    st.caption(f"👍 {item['upvote_count']} · sign in to upvote")
                elif has_upvoted_news(news_id, st.session_state.current_user):
                    st.caption(f"✅ {item['upvote_count']} · you upvoted this")
                else:
                    if st.button(f"👍 {item['upvote_count']}", key=f"news_upvote_{news_id}"):
                        if upvote_news(news_id, st.session_state.current_user):
                            add_civic_points(st.session_state.current_user, 1)
                        st.rerun()

        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("⬅ Prev", disabled=(page <= 1)):
                st.session_state.news_page -= 1
                st.rerun()
        with col_info:
            st.markdown(
                f"<div style='text-align:center; padding-top:6px;'>Page {page} of {total_pages} "
                f"({len(all_news)} articles)</div>",
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("Next ➡", disabled=(page >= total_pages)):
                st.session_state.news_page += 1
                st.rerun()

"""
RoadPulse - "Friendly Neighborhood Road Watch // Spidey Edition"
A sleek, modern civic platform prototype built with Streamlit + Google Maps + SQLite + Groq.

Run with:
    pip install -r requirements.txt
    streamlit run roadpulse_app.py
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
# MUNICIPAL DIRECTORY & ROUTING
# --------------------------------------------------------------------------
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
GCC_DETAILS_MAX_CHARS = 400

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

SUBMISSION_COOLDOWN_SECONDS = 120
DUPLICATE_RADIUS_METERS = 40


@st.cache_data(ttl=3600)
def reverse_geocode(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "addressdetails": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Spidey-Watch/3.0"})
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
    district_name, country_code = reverse_geocode(round(row["lat"], 3), round(row["lon"], 3))

    if district_name:
        key = district_name.lower()
        for name_key, info in MUNICIPALITY_DIRECTORY.items():
            if name_key in key:
                return info, True

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

st.set_page_config(
    page_title="RoadPulse · Spidey Watch Edition",
    page_icon="🕷️",
    layout="wide",
    initial_sidebar_state="expanded"
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# SPIDER-VERSE // CYBER-SUIT HUD PREMIUM THEME
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&family=Cinzel:wght@700;900&display=swap');

    :root {
        --spidey-red: #FF2A54;
        --spidey-red-glow: rgba(255, 42, 84, 0.35);
        --spidey-blue: #00D2FF;
        --spidey-blue-glow: rgba(0, 210, 255, 0.25);
        --spidey-dark: #070A12;
        --spidey-card: rgba(15, 20, 36, 0.78);
        --spidey-border: rgba(255, 255, 255, 0.09);
        --text-pure: #FFFFFF;
        --text-dim: #94A3B8;
    }

    * {
        font-family: 'Plus Jakarta Sans', sans-serif;
    }

    /* Ambient Spider-Verse Mesh Glow + Web Lattice Pattern */
    .stApp {
        background:
            radial-gradient(ellipse 60% 40% at 50% -10%, rgba(255, 42, 84, 0.18) 0%, transparent 60%),
            radial-gradient(circle at 10% 30%, rgba(0, 210, 255, 0.1) 0%, transparent 45%),
            radial-gradient(circle at 90% 70%, rgba(255, 42, 84, 0.08) 0%, transparent 45%),
            repeating-radial-gradient(circle at 50% 50%,
                rgba(255, 42, 84, 0.02) 0px, rgba(255, 42, 84, 0.02) 1px,
                transparent 1px, transparent 60px),
            #070A12;
        color: var(--text-pure);
    }

    /* Headings */
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 800 !important;
        letter-spacing: -0.02em !important;
        color: #FFFFFF !important;
        text-shadow: 0 0 20px rgba(255, 42, 84, 0.2) !important;
    }

    /* Spidey Hero Header */
    .hero-header {
        position: relative;
        background: linear-gradient(135deg, rgba(28, 14, 28, 0.85) 0%, rgba(11, 18, 38, 0.88) 100%);
        border: 1px solid rgba(255, 42, 84, 0.25);
        border-radius: 20px;
        padding: 28px 34px;
        margin-bottom: 24px;
        backdrop-filter: blur(20px);
        box-shadow: 0 16px 40px -10px rgba(0, 0, 0, 0.6), 0 0 30px rgba(255, 42, 84, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.15);
        overflow: hidden;
    }
    .hero-header::before {
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0; height: 3px;
        background: linear-gradient(90deg, transparent, #FF2A54, #00D2FF, #FF2A54, transparent);
    }
    .spidey-radar-badge {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #FF2A54;
        background: rgba(255, 42, 84, 0.12);
        border: 1px solid rgba(255, 42, 84, 0.4);
        padding: 5px 14px;
        border-radius: 9999px;
        margin-bottom: 12px;
        box-shadow: 0 0 14px rgba(255, 42, 84, 0.25);
    }
    .hero-title {
        font-size: 2.25rem !important;
        font-weight: 900 !important;
        background: linear-gradient(135deg, #FFFFFF 20%, #FF8099 70%, #00D2FF 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin: 0 0 8px 0 !important;
    }
    .hero-subtitle {
        color: #CBD5E1;
        font-size: 0.98rem;
        margin: 0;
        max-width: 680px;
        line-height: 1.55;
    }

    /* Sidebar HUD Styling */
    section[data-testid="stSidebar"] {
        background: rgba(10, 14, 26, 0.95) !important;
        border-right: 1px solid rgba(255, 42, 84, 0.2) !important;
        backdrop-filter: blur(24px);
    }
    section[data-testid="stSidebar"] hr {
        border-color: rgba(255, 255, 255, 0.08) !important;
        margin: 18px 0;
    }

    /* Spidey Profile Card */
    .spidey-profile-card {
        background: linear-gradient(135deg, rgba(38, 14, 28, 0.75) 0%, rgba(13, 22, 45, 0.85) 100%);
        border: 1px solid rgba(255, 42, 84, 0.35);
        border-radius: 16px;
        padding: 16px 18px;
        margin-bottom: 14px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35), 0 0 16px rgba(255, 42, 84, 0.15);
    }
    .spidey-points-pill {
        display: inline-block;
        background: linear-gradient(135deg, rgba(255, 42, 84, 0.2) 0%, rgba(0, 210, 255, 0.2) 100%);
        color: #FFFFFF;
        border: 1px solid rgba(255, 255, 255, 0.25);
        padding: 3px 12px;
        border-radius: 9999px;
        font-size: 12px;
        font-weight: 800;
        box-shadow: 0 0 10px rgba(0, 210, 255, 0.2);
    }

    /* Tabs Styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 10px;
        background: rgba(14, 18, 32, 0.75);
        padding: 6px;
        border-radius: 14px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        margin-bottom: 22px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px !important;
        padding: 10px 20px !important;
        font-weight: 700 !important;
        font-size: 13px !important;
        letter-spacing: 0.02em;
        color: var(--text-dim) !important;
        border: none !important;
        background: transparent !important;
        transition: all 0.25s ease;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, rgba(255, 42, 84, 0.25) 0%, rgba(0, 210, 255, 0.18) 100%) !important;
        color: #FFFFFF !important;
        border: 1px solid rgba(255, 42, 84, 0.45) !important;
        box-shadow: 0 4px 16px rgba(255, 42, 84, 0.25) !important;
    }

    /* Dual-Tone Action Buttons */
    div.stButton > button, div.stLinkButton > a, .stForm button {
        background: linear-gradient(135deg, #FF2A54 0%, #B80036 100%) !important;
        color: #FFFFFF !important;
        border: 1px solid rgba(255, 255, 255, 0.25) !important;
        border-radius: 11px !important;
        padding: 9px 20px !important;
        font-weight: 700 !important;
        font-size: 13px !important;
        letter-spacing: 0.02em;
        box-shadow: 0 4px 16px rgba(255, 42, 84, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.3) !important;
        transition: all 0.2s ease !important;
    }
    div.stButton > button:hover, div.stLinkButton > a:hover, .stForm button:hover {
        background: linear-gradient(135deg, #FF456A 0%, #D6003E 100%) !important;
        border-color: rgba(255, 255, 255, 0.4) !important;
        box-shadow: 0 6px 24px rgba(255, 42, 84, 0.55) !important;
        transform: translateY(-2px);
    }

    /* Modern Glassmorphic Expanders */
    div[data-testid="stExpander"] {
        background: var(--spidey-card) !important;
        border: 1px solid var(--spidey-border) !important;
        border-radius: 14px !important;
        box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35) !important;
        margin-bottom: 14px !important;
        transition: all 0.2s ease;
    }
    div[data-testid="stExpander"]:hover {
        border-color: rgba(255, 42, 84, 0.35) !important;
    }

    /* Metrics Styling */
    div[data-testid="stMetricValue"] {
        font-size: 1.7rem !important;
        font-weight: 900 !important;
        background: linear-gradient(135deg, #FFFFFF 40%, #FF8099 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    div[data-testid="stMetricLabel"] {
        font-size: 0.82rem !important;
        font-weight: 700 !important;
        color: #94A3B8 !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    /* Badges */
    .spidey-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 12px;
        border-radius: 9999px;
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 0.03em;
    }
    .spidey-pill-open {
        background: rgba(255, 42, 84, 0.15);
        color: #FF5C7A;
        border: 1px solid rgba(255, 42, 84, 0.4);
    }
    .spidey-pill-fixed {
        background: rgba(0, 210, 255, 0.15);
        color: #38BDF8;
        border: 1px solid rgba(0, 210, 255, 0.4);
    }

    /* Daily Bugle Header & Articles */
    .bugle-masthead {
        background: linear-gradient(135deg, rgba(20, 10, 10, 0.9) 0%, rgba(10, 15, 30, 0.9) 100%);
        border: 2px solid #FF2A54;
        border-radius: 14px;
        padding: 16px;
        margin-bottom: 20px;
        text-align: center;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5), inset 0 0 20px rgba(255, 42, 84, 0.1);
    }
    .bugle-title {
        font-family: 'Cinzel', serif !important;
        font-size: 2.2rem !important;
        font-weight: 900 !important;
        letter-spacing: 3px;
        color: #F8FAFC !important;
        margin: 0 !important;
        text-shadow: 0 0 10px rgba(255, 42, 84, 0.5) !important;
    }
    .bugle-tagline {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        letter-spacing: 2px;
        color: #00D2FF;
        text-transform: uppercase;
        margin-top: 4px;
    }
    .news-card {
        background: linear-gradient(135deg, rgba(18, 24, 44, 0.8) 0%, rgba(12, 16, 32, 0.9) 100%);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 14px;
        padding: 18px 22px;
        margin-bottom: 14px;
        transition: all 0.2s ease;
    }
    .news-card:hover {
        border-color: rgba(0, 210, 255, 0.4);
        box-shadow: 0 8px 28px rgba(0, 0, 0, 0.4), 0 0 16px rgba(0, 210, 255, 0.12);
        transform: translateY(-1px);
    }
    .news-title {
        font-size: 1.05rem;
        font-weight: 700;
        color: #FFFFFF;
        margin-bottom: 6px;
    }
    .news-meta {
        font-size: 0.82rem;
        color: #94A3B8;
    }

    /* ---------------------------------------------------------------- */
    /* INSTAGRAM-STYLE LEFT NAV                                          */
    /* ---------------------------------------------------------------- */
    .ig-logo {
        font-size: 1.35rem;
        font-weight: 900;
        letter-spacing: -0.02em;
        background: linear-gradient(135deg, #FFFFFF 20%, #FF8099 70%, #00D2FF 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        padding: 6px 4px 14px 4px;
        margin-bottom: 6px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] {
        gap: 2px !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        width: 100%;
        padding: 11px 14px !important;
        border-radius: 12px !important;
        font-size: 15px !important;
        font-weight: 600 !important;
        color: var(--text-dim) !important;
        transition: all 0.2s ease;
        border: 1px solid transparent;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        background: rgba(255, 255, 255, 0.05) !important;
        color: #FFFFFF !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
        background: linear-gradient(90deg, rgba(255, 42, 84, 0.28), rgba(0, 210, 255, 0.12)) !important;
        border: 1px solid rgba(255, 42, 84, 0.45) !important;
        color: #FFFFFF !important;
        font-weight: 800 !important;
        box-shadow: 0 4px 14px rgba(255, 42, 84, 0.25);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {
        display: none !important;
    }

    /* ---------------------------------------------------------------- */
    /* TOP-RIGHT FLOATING CIVIC SCORE BADGE                               */
    /* ---------------------------------------------------------------- */
    .ig-top-score {
        position: fixed;
        top: 18px;
        right: 34px;
        z-index: 999999;
        display: flex;
        align-items: center;
        gap: 8px;
        background: linear-gradient(135deg, rgba(255, 42, 84, 0.22) 0%, rgba(0, 210, 255, 0.18) 100%);
        border: 1px solid rgba(255, 255, 255, 0.25);
        backdrop-filter: blur(16px);
        padding: 8px 18px;
        border-radius: 9999px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.4), 0 0 16px rgba(255, 42, 84, 0.2);
    }
    .ig-top-score .star { color: #FFD166; font-size: 15px; }
    .ig-top-score .num { font-weight: 900; font-size: 15px; color: #FFFFFF; }
    .ig-top-score .lbl { font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: #CBD5E1; }

    /* ---------------------------------------------------------------- */
    /* NEWS TICKER BAR                                                    */
    /* ---------------------------------------------------------------- */
    .news-ticker-bar {
        display: flex;
        align-items: center;
        background: linear-gradient(90deg, rgba(255,42,84,0.16), rgba(0,210,255,0.08));
        border: 1px solid rgba(255, 42, 84, 0.35);
        border-radius: 10px;
        margin-bottom: 20px;
        overflow: hidden;
        height: 38px;
    }
    .news-ticker-tag {
        flex-shrink: 0;
        background: #FF2A54;
        color: #fff;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 900;
        font-size: 11px;
        letter-spacing: 1.5px;
        padding: 8px 14px;
        height: 100%;
        display: flex;
        align-items: center;
    }
    .news-ticker-viewport {
        overflow: hidden;
        white-space: nowrap;
        flex: 1;
    }
    .news-ticker-track {
        display: inline-block;
        padding-left: 100%;
        animation: ticker-scroll 55s linear infinite;
        font-family: 'JetBrains Mono', monospace;
        font-size: 13px;
        font-weight: 500;
        color: #E2E8F0;
    }
    .news-ticker-track span.hl-sep { color: #FF5C7A; margin: 0 28px; font-weight: 900; }
    @keyframes ticker-scroll {
        0% { transform: translateX(0); }
        100% { transform: translateX(-100%); }
    }

    /* ---------------------------------------------------------------- */
    /* DASHBOARD CARDS                                                    */
    /* ---------------------------------------------------------------- */
    .ig-dash-card {
        background: var(--spidey-card);
        border: 1px solid var(--spidey-border);
        border-radius: 16px;
        padding: 18px 20px;
        margin-bottom: 12px;
    }
    .ig-activity-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 4px;
        border-bottom: 1px solid rgba(255,255,255,0.06);
    }
    .ig-activity-row:last-child { border-bottom: none; }

    /* ---------------------------------------------------------------- */
    /* PROFILE (INSTAGRAM STYLE)                                         */
    /* ---------------------------------------------------------------- */
    .ig-profile-header {
        display: flex;
        align-items: center;
        gap: 26px;
        padding: 20px 6px 10px 6px;
    }
    .ig-avatar {
        width: 88px;
        height: 88px;
        border-radius: 50%;
        background: linear-gradient(135deg, #FF2A54, #00D2FF);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 2.2rem;
        font-weight: 900;
        color: #fff;
        flex-shrink: 0;
        box-shadow: 0 0 0 4px rgba(255,42,84,0.25);
    }
    .ig-profile-name { font-size: 1.4rem; font-weight: 800; color: #fff; margin-bottom: 4px; }
    .ig-profile-badge { font-size: 12px; color: #FF8099; font-weight: 700; margin-bottom: 10px; }
    .ig-stats-row { display: flex; gap: 34px; margin-top: 6px; }
    .ig-stat-num { font-size: 1.1rem; font-weight: 900; color: #fff; }
    .ig-stat-lbl { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; }
    .ig-post-card {
        background: var(--spidey-card);
        border: 1px solid var(--spidey-border);
        border-radius: 14px;
        overflow: hidden;
        margin-bottom: 14px;
    }
    .ig-post-meta { padding: 10px 12px; }
    .ig-post-loc { font-size: 12.5px; font-weight: 700; color: #fff; margin-bottom: 3px; }
    .ig-post-date { font-size: 11px; color: var(--text-dim); }
    </style>
    """,
    unsafe_allow_html=True,
)

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
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
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

    cursor.execute("SELECT id, published FROM news_items")
    for news_id, published_value in cursor.fetchall():
        if not published_value:
            continue
        try:
            datetime.strptime(published_value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                fixed_dt = parsedate_to_datetime(published_value)
                cursor.execute(
                    "UPDATE news_items SET published = ? WHERE id = ?",
                    (fixed_dt.strftime("%Y-%m-%d %H:%M:%S"), news_id),
                )
            except (TypeError, ValueError):
                pass

    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# AUTHENTICATION
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
        return False, "That citizen handle is already taken."
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
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = "(no response body)"
        return None, f"Google sign-in failed: HTTP {exc.code} - {body}"
    except Exception as exc:
        return None, f"Google sign-in failed: {exc}"


def find_username_by_email(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def create_google_user(username, email):
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
        return False, "That handle is taken - try another."
    finally:
        conn.close()


def civic_badge(score):
    if score >= 100:
        return "🕸️🦸 Amazing Spider-Citizen"
    elif score >= 50:
        return "🦸 Spectacular Web-Slinger"
    elif score >= 20:
        return "🕷️ Web-Slinger in Training"
    else:
        return "🏙️ Friendly Neighborhood Sentinel"


# --------------------------------------------------------------------------
# REVIEW / DATA HELPERS
# --------------------------------------------------------------------------
def get_seconds_since_last_submission(username):
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
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def find_nearby_duplicate(lat, lon, category):
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
# COMMUNITY NEWS FEED
# --------------------------------------------------------------------------
def fetch_news_rss(query, max_items=10):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Spidey-Watch/3.0"})
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
        source = source_el.text if source_el is not None else "Daily Bugle Wire"

        try:
            published_dt = parsedate_to_datetime(raw_pub_date)
        except (TypeError, ValueError):
            published_dt = datetime.now()
        published_iso = published_dt.strftime("%Y-%m-%d %H:%M:%S")

        if link:
            items.append({"title": title, "link": link, "source": source, "published": published_iso})
    return items


def store_news_items(items, search_query):
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
            cursor.execute("UPDATE news_items SET search_query = ? WHERE link = ?", (search_query, item["link"]))
    conn.commit()
    conn.close()


def fetch_news_for_query(search_query, limit=200):
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
# GOOGLE OAUTH CALLBACK
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
    return value if isinstance(value, str) else default


def safe_image(path, **kwargs):
    if isinstance(path, str) and os.path.isfile(path):
        st.image(path, **kwargs)
    else:
        st.caption("📷 Image preview unavailable.")


# --------------------------------------------------------------------------
# GROQ AI CONFIG & HELPERS (Smart Multi-Model Fallback)
# --------------------------------------------------------------------------
GROQ_VISION_MODELS = [
    m for m in [
        os.environ.get("GROQ_VISION_MODEL", "").strip(),
        "llama-3.2-11b-vision-preview",
        "llama-3.2-90b-vision-preview",
        "qwen/qwen3.6-27b",
        "qwen/qwen3-vl-32b-instruct",
    ] if m
]

GROQ_TEXT_MODELS = [
    m for m in [
        os.environ.get("GROQ_TEXT_MODEL", "").strip(),
        "qwen/qwen3.6-27b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ] if m
]

SEVERITY_TO_RATING = {"Mild": 3, "Severe": 2, "Critical": 1}


def analyze_road_photo(image_bytes, mime_type="image/jpeg"):
    try:
        from groq import Groq
    except ImportError:
        return None, "Install the 'groq' package (pip install groq)."

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "Set the GROQ_API_KEY environment variable."

    prompt = (
        "You are verifying a citizen-submitted photo for a civic road-damage platform. "
        "Filter out memes, jokes, or non-road images. Respond with ONLY a JSON object: "
        '{"is_road_issue": true or false, '
        '"category": one of ["Pothole","Waterlogging","Traffic/Cracks"] or null, '
        '"severity": one of ["Mild","Severe","Critical"] or null, '
        '"looks_ai_generated": false, '
        '"explanation": "one concise sentence reason"}.'
    )

    client = Groq(api_key=api_key)
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{base64_image}"

    last_error = None
    for model_name in GROQ_VISION_MODELS:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                temperature=0.0,
            )

            raw_text = response.choices[0].message.content.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.strip("`")
                if raw_text.lower().startswith("json"):
                    raw_text = raw_text[4:]
            data = json.loads(raw_text.strip())
            return data, None
        except Exception as exc:
            last_error = exc
            continue

    return None, f"Vision analysis failed: {last_error}"


def polish_complaint_with_ai(details_text, category, rating, location_name):
    try:
        from groq import Groq
    except ImportError:
        return None, "Install the 'groq' package (pip install groq)."

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "Set the GROQ_API_KEY environment variable."

    prompt = (
        "Rewrite this citizen road complaint description as a formal, persuasive grievance "
        "for a municipal portal. Keep GPS coordinates, location names, and core facts. "
        f"Respond with ONLY the rewritten text under {GCC_DETAILS_MAX_CHARS} characters, no quotation marks.\n\n"
        f"Category: {category}\nRating: {rating}/5\nLocation: {location_name}\n"
        f"Original text: {details_text}"
    )

    client = Groq(api_key=api_key)
    last_error = None
    for model_name in GROQ_TEXT_MODELS:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            polished = response.choices[0].message.content.strip().strip('"')
            return polished[:GCC_DETAILS_MAX_CHARS], None
        except Exception as exc:
            last_error = exc
            continue

    return None, f"AI rewrite failed: {last_error}"


def build_civic_complaint(row, municipality_info, is_known_portal):
    if is_known_portal and municipality_info.get("categories"):
        info = municipality_info["categories"].get(row["category"], municipality_info["categories"]["Pothole"])
        group, complaint_type = info["group"], info["type"]
    else:
        group, complaint_type = "Roads & Infrastructure", row["category"]

    title = f"Road Defect Report - {row['location_name']}"[:80]
    description = safe_str(row["description"], "Citizen submitted report via RoadPulse.")
    prefix = f"[{row['category']}] At {row['location_name']} (GPS: {row['lat']:.5f}, {row['lon']:.5f}). "
    details = (prefix + description)[:GCC_DETAILS_MAX_CHARS]

    return {"group": group, "type": complaint_type, "title": title, "details": details}


def geocode_location(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Spidey-Watch/3.0"})
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
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 0}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RoadPulse-Spidey-Watch/3.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return f"Near {lat:.5f}, {lon:.5f}"
    return data.get("display_name") or f"Near {lat:.5f}, {lon:.5f}"


def reviews_to_payload(df):
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
            "path_coords": json.loads(row["path_coords"]) if isinstance(row["path_coords"], str) else None,
        })
    return records


# --------------------------------------------------------------------------
# SIDEBAR - INSTAGRAM-STYLE LEFT NAVIGATION
# --------------------------------------------------------------------------
st.sidebar.markdown('<div class="ig-logo">🕷️ RoadPulse</div>', unsafe_allow_html=True)

NAV_ITEMS = ["🏠  Dashboard", "🧭  Map", "👥  Community", "📰  News Feed", "👤  Profile"]
if "nav_page" not in st.session_state:
    st.session_state.nav_page = NAV_ITEMS[0]

_nav_choice = st.sidebar.radio(
    "Navigate", NAV_ITEMS, label_visibility="collapsed",
    index=NAV_ITEMS.index(st.session_state.nav_page), key="nav_radio",
)
st.session_state.nav_page = _nav_choice
nav_page = _nav_choice.split("  ", 1)[1].strip()

st.sidebar.markdown("---")

# --------------------------------------------------------------------------
# SIDEBAR - ACCOUNT
# --------------------------------------------------------------------------
if st.session_state.current_user is None:
    st.sidebar.subheader("🕷️ Web-Watch Sign In")

    if st.session_state.get("_oauth_error"):
        st.sidebar.error(st.session_state._oauth_error)
        st.session_state._oauth_error = None

    if st.session_state.get("_pending_google_email"):
        pending_email = st.session_state._pending_google_email
        st.sidebar.success(f"Authenticated as {pending_email}")
        chosen_username = st.sidebar.text_input("Pick a Spidey Handle", key="google_username_choice")
        if st.sidebar.button("Join the Web"):
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
            st.sidebar.caption("Google sign-in optional.")

        with st.sidebar.expander("Local Citizen Account"):
            login_tab, signup_tab = st.tabs(["Sign In", "Sign Up"])

            with login_tab:
                login_username = st.text_input("Handle", key="login_username")
                login_password = st.text_input("Password", type="password", key="login_password")
                if st.button("Sign In", key="login_btn"):
                    if verify_user(login_username.strip(), login_password):
                        st.session_state.current_user = login_username.strip()
                        st.rerun()
                    else:
                        st.error("Invalid credentials.")

            with signup_tab:
                signup_username = st.text_input("Choose Handle", key="signup_username")
                signup_password = st.text_input("Password", type="password", key="signup_password")
                if st.button("Create Account", key="signup_btn"):
                    if not signup_username.strip() or not signup_password:
                        st.error("Both fields required.")
                    else:
                        ok, err = create_user(signup_username.strip(), signup_password)
                        if ok:
                            st.session_state.current_user = signup_username.strip()
                            st.rerun()
                        else:
                            st.error(err)
else:
    score = get_civic_score(st.session_state.current_user)
    st.sidebar.markdown(
        f"""
        <div class="spidey-profile-card">
            <div style="font-weight:800; font-size:1.05rem; margin-bottom:4px;">🕷️ {st.session_state.current_user}</div>
            <div style="font-size:12px; font-weight:600; color:#FF8099;">{civic_badge(score)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Log Out"):
        st.session_state.current_user = None
        st.rerun()

    # Floating top-right civic score badge (Instagram-style top bar)
    st.markdown(
        f"""
        <div class="ig-top-score">
            <span class="star">★</span>
            <div>
                <div class="num">{score}</div>
                <div class="lbl">Civic Score</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# --------------------------------------------------------------------------
# MAIN UI
# --------------------------------------------------------------------------
st.markdown(
    """
    <div class="hero-header">
        <div class="spidey-radar-badge">🕷️ SPIDEY-SENSE: ACTIVE & MONITORING</div>
        <h1 class="hero-title">RoadPulse · Friendly Neighborhood Watch</h1>
        <p class="hero-subtitle">
            With great roads comes great responsibility. Spot hazards, verify with AI,
            and sling grievances directly to your municipal corporation.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not GOOGLE_MAPS_API_KEY:
    st.warning("Google Maps API key not detected. Set GOOGLE_MAPS_API_KEY in environment.")

# --------------------------------------------------------------------------
# PAGE ROUTER (Instagram-style left-nav pages)
# --------------------------------------------------------------------------

# ----- PAGE: DASHBOARD -------
if nav_page == "Dashboard":
    dash_df = fetch_all_reviews()

    col_d1, col_d2, col_d3, col_d4 = st.columns(4)
    with col_d1:
        st.metric("Total Reports", len(dash_df))
    with col_d2:
        open_count = int((dash_df["status"] == "Open").sum()) if not dash_df.empty else 0
        st.metric("Open Hazards", open_count)
    with col_d3:
        fixed_count = int((dash_df["status"] != "Open").sum()) if not dash_df.empty else 0
        st.metric("Fixed", fixed_count)
    with col_d4:
        if st.session_state.current_user:
            st.metric("Your Civic Score", get_civic_score(st.session_state.current_user))
        else:
            st.metric("Avg. Road Rating", f"{dash_df['rating'].mean():.1f} ⭐" if not dash_df.empty else "—")

    st.markdown("")
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.markdown("##### 🕸️ Recent Neighborhood Activity")
        if dash_df.empty:
            st.info("No hazards reported yet. Head to the Map tab to sling the first one!")
        else:
            recent = dash_df.sort_values("timestamp", ascending=False).head(6)
            for _, row in recent.iterrows():
                pill_class = "spidey-pill-open" if row["status"] == "Open" else "spidey-pill-fixed"
                st.markdown(
                    f"""
                    <div class="ig-activity-row">
                        <div>
                            <div style="font-weight:700; font-size:13.5px;">{row['category']} · {row['location_name'][:48]}</div>
                            <div style="font-size:11.5px; color:#94A3B8;">reported by {row['username']} · {row['timestamp']}</div>
                        </div>
                        <span class="spidey-pill {pill_class}">{row['status']}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    with col_right:
        st.markdown("##### 🏆 Top Web-Slingers")
        conn_lead = get_connection()
        leaderboard_df = pd.read_sql_query(
            "SELECT username, civic_score FROM users ORDER BY civic_score DESC LIMIT 5", conn_lead
        )
        conn_lead.close()
        if leaderboard_df.empty:
            st.caption("No citizens on the board yet.")
        else:
            for i, lb_row in leaderboard_df.iterrows():
                medal = ["🥇", "🥈", "🥉", "🏅", "🏅"][i] if i < 5 else "🏅"
                st.markdown(
                    f"""
                    <div class="ig-activity-row">
                        <div style="font-weight:700; font-size:13px;">{medal} {lb_row['username']}</div>
                        <span style="font-weight:800; font-size:13px; color:#FF8099;">{lb_row['civic_score']} pts</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.markdown("")
        st.caption("Head to **🧭 Map** to spot and report a new road hazard.")

# ----- PAGE: MAP -------
elif nav_page == "Map":
    reviews_df = fetch_all_reviews()

    col_view, col_blank = st.columns([2, 4])
    with col_view:
        view_choice = st.segmented_control(
            "Radar View",
            ["📍 Live Explorer", "🔥 Hazard Heatmap"],
            default="📍 Live Explorer",
            label_visibility="collapsed"
        )
    is_heatmap_view = "Heatmap" in str(view_choice)

    col_map, col_stats = st.columns([3.2, 1])

    with col_stats:
        st.metric("Total Hazards Trapped", len(reviews_df))
        if not reviews_df.empty:
            st.metric("Mean Road Quality", f"{reviews_df['rating'].mean():.1f} ⭐")
            open_severe = ((reviews_df["rating"] <= 2) & (reviews_df["status"] == "Open")).sum()
            st.metric("Severe Open Hazards", int(open_severe))

        if is_heatmap_view:
            st.caption("🔴 Red = Critical Unfixed &nbsp;|&nbsp; 🟢 Green = Fixed")
        else:
            st.caption("Use the ✏️ Draw Segment tool on top-right of the map to trace road spans.")

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
            st.info("Map awaiting Google Maps API key credentials.")

    st.markdown("---")
    with st.expander("📝 File a Complaint", expanded=False):
        if st.session_state.current_user is None:
            st.info("Sign in from the sidebar to file a complaint.")
        else:
            st.subheader("📍 Web-Sling Coordinates")
    
            if st.session_state.segment_coords:
                points = st.session_state.segment_coords
                centroid_lat = sum(p[0] for p in points) / len(points)
                centroid_lon = sum(p[1] for p in points) / len(points)
                st.success(f"🧵 Segment Locked ({len(points)} pts)")
                st.caption(f"📍 {get_location_name(centroid_lat, centroid_lon)}")
            elif st.session_state.clicked_lat is not None:
                st.success("🎯 Pin Locked")
                st.caption(f"📍 {get_location_name(st.session_state.clicked_lat, st.session_state.clicked_lon)}")
            else:
                st.caption("Click map or use Spidey radar tools below.")
    
            st.markdown("**🕸️ Spidey Radar & Search**")
            geo_result = geo_component(key="geo_button", default=None)
            if geo_result and geo_result != st.session_state._last_geo_raw:
                st.session_state._last_geo_raw = geo_result
                st.session_state.clicked_lat = geo_result["latitude"]
                st.session_state.clicked_lon = geo_result["longitude"]
                st.session_state.segment_coords = None
                st.session_state.map_center = [geo_result["latitude"], geo_result["longitude"]]
    
            search_query = st.text_input("Find Neighborhood / Street", placeholder="e.g. T Nagar, Chennai")
            if st.button("🔎 Locate"):
                if search_query.strip():
                    result = geocode_location(search_query.strip())
                    if result:
                        st.session_state.clicked_lat, st.session_state.clicked_lon = result
                        st.session_state.segment_coords = None
                        st.session_state.map_center = list(result)
                        st.success(f"Found: {result[0]:.4f}, {result[1]:.4f}")
                    else:
                        st.error("Location not found.")
                else:
                    st.warning("Enter a location to search.")
    
            if st.button("Reset Pin"):
                st.session_state.clicked_lat = None
                st.session_state.clicked_lon = None
                st.session_state.segment_coords = None
                st.rerun()
    
            st.markdown("---")
    
            st.subheader("📝 Report Hazard")
    
            if st.session_state.current_user is None:
                st.info("Sign in above to sling reports onto the web.")
            else:
                st.caption("📸 AI Vision Damage Verification")
                photo_method = st.radio(
                    "Capture Mode", ["📷 Live Cam", "📁 Device File"],
                    horizontal=True, label_visibility="collapsed", key="review_photo_method",
                )
                if photo_method.startswith("📷"):
                    uploaded_photo = st.camera_input("Shoot Road Photo", key="review_photo_camera")
                else:
                    uploaded_photo = st.file_uploader(
                        "Upload Road Photo", type=["jpg", "jpeg", "png"], key="review_photo_uploader"
                    )
                if uploaded_photo is not None:
                    if st.button("🕷️ Scan Photo with AI"):
                        photo_bytes = uploaded_photo.getvalue()
                        mime_type = uploaded_photo.type or "image/jpeg"
                        with st.spinner("Spidey-sense scanning photo..."):
                            result, ai_error = analyze_road_photo(photo_bytes, mime_type)
                        if ai_error:
                            st.warning(ai_error)
                        elif not result.get("is_road_issue"):
                            st.warning(f"⚠️ False Alarm: {result.get('explanation')}")
                            st.session_state.ai_category = None
                            st.session_state.ai_rating = None
                            st.session_state.ai_explanation = None
                        else:
                            st.session_state.ai_category = result.get("category")
                            st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                            st.session_state.ai_explanation = result.get("explanation")
                            st.success(f"Verified: {result.get('category')} ({result.get('severity')})")
                        st.session_state.pending_photo_bytes = photo_bytes
    
                    if st.session_state.get("ai_explanation"):
                        st.caption(f"🤖 AI Verdict: {st.session_state.ai_explanation}")
    
                default_category_index = (
                    CATEGORIES.index(st.session_state.get("ai_category"))
                    if st.session_state.get("ai_category") in CATEGORIES else 0
                )
                default_rating = st.session_state.get("ai_rating") or 3
    
                with st.form("review_form", clear_on_submit=True):
                    category = st.selectbox("Category", CATEGORIES, index=default_category_index)
                    rating = st.slider("Hazard Level (1 = Dangerous, 5 = Smooth)", 1, 5, default_rating)
                    description = st.text_area("Hazard Details", placeholder="Describe the potholes, cracks, or waterlogging...")
                    submitted = st.form_submit_button("🕸️ Thwip! Submit Report")
    
                    if submitted:
                        has_point = st.session_state.clicked_lat is not None
                        has_segment = bool(st.session_state.segment_coords)
                        cooldown_elapsed = get_seconds_since_last_submission(st.session_state.current_user)
                        in_cooldown = cooldown_elapsed is not None and cooldown_elapsed < SUBMISSION_COOLDOWN_SECONDS
    
                        if not has_point and not has_segment:
                            st.error("Please pin or draw a location on the map first.")
                        elif in_cooldown:
                            wait_seconds = int(SUBMISSION_COOLDOWN_SECONDS - cooldown_elapsed)
                            st.error(f"⏱️ Slow down web-slinger! Wait {wait_seconds}s before submitting.")
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
                                st.warning(f"A report for '{category}' already exists nearby at '{duplicate['location_name']}'. Please endorse that one instead!")
                            else:
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
                                st.success("🕸️ *Thwip!* Hazard added to the neighborhood web! (+10 Civic Score)")
    
                                for key in ("clicked_lat", "clicked_lon", "segment_coords", "_last_picker_raw", "_last_geo_raw"):
                                    st.session_state[key] = defaults[key]
                                for key in ("ai_category", "ai_rating", "ai_explanation", "pending_photo_bytes"):
                                    st.session_state[key] = None
                                st.rerun()

# ----- PAGE: COMMUNITY -------
elif nav_page == "Community":
    st.subheader("💬 Community Web Feed")
    st.caption("All reported road defects, photo evidence, civic endorsements, and repair confirmations.")

    all_reviews = fetch_all_reviews()

    if all_reviews.empty:
        st.info("No hazards on the web yet. Pin a road in the sidebar to report one!")
    else:
        for _, row in all_reviews.iterrows():
            review_id = int(row["id"])
            is_segment = isinstance(row["path_coords"], str)
            loc_icon = "🧵 Corridor" if is_segment else "📍 Spot"
            status_html = '<span class="spidey-pill spidey-pill-fixed">🟢 FIXED</span>' if row["status"] == "Fixed" else '<span class="spidey-pill spidey-pill-open">🔴 OPEN</span>'
            stars = "⭐" * int(row["rating"])
            ticket_id = f"RP-{review_id:04d}"

            with st.expander(f"{ticket_id} · {loc_icon} {row['location_name']} · {stars}"):
                st.markdown(
                    f"<div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;'>"
                    f"<span><b>Category:</b> {row['category']} · <b>Reported by:</b> 🕷️ {row['username']}</span>"
                    f"{status_html}"
                    f"</div>",
                    unsafe_allow_html=True
                )
                st.write(safe_str(row["description"], "_No description provided._"))

                if row["status"] == "Open" and int(row["rating"]) <= 2:
                    reported_dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    days_elapsed = (datetime.now() - reported_dt).days
                    if days_elapsed > SLA_DAYS:
                        st.error(f"⛔ SLA Breached! Reported {days_elapsed} days ago (Exceeds {SLA_DAYS}-day repair clock).")
                    else:
                        st.info(f"⏳ Repair SLA Clock: {SLA_DAYS - days_elapsed} day(s) remaining for municipal resolution.")

                # Municipal Grievance Routing
                if row["status"] == "Open":
                    with st.expander("📋 Sling this Grievance to Municipal Public Works"):
                        municipality_info, is_known_portal = get_municipality_info(row)
                        st.markdown(f"**Target Jurisdiction:** `{municipality_info['name']}`")
                        civic = build_civic_complaint(row, municipality_info, is_known_portal)

                        col_g1, col_g2 = st.columns(2)
                        with col_g1:
                            st.caption("Complaint Category")
                            st.code(civic["group"], language=None)
                        with col_g2:
                            st.caption("Complaint Type")
                            st.code(civic["type"], language=None)

                        polish_key = f"ai_polished_{review_id}"
                        if st.button("✨ Polish Grievance with AI", key=f"ai_polish_btn_{review_id}"):
                            with st.spinner("Crafting formal complaint wording..."):
                                polished, ai_error = polish_complaint_with_ai(
                                    civic["details"], row["category"], int(row["rating"]), row["location_name"]
                                )
                            if ai_error:
                                st.warning(ai_error)
                            else:
                                st.session_state[polish_key] = polished

                        details_to_show = st.session_state.get(polish_key, civic["details"])
                        st.caption(f"Complaint Description ({len(details_to_show)}/{GCC_DETAILS_MAX_CHARS} chars):")
                        st.code(details_to_show, language=None)

                        if row["photo_path"] and isinstance(row["photo_path"], str):
                            safe_image(row["photo_path"], width=240, caption="Evidence Photo")

                        btn_title = "Open Official Portal ↗" if is_known_portal else "Search Regional Portal ↗"
                        st.link_button(btn_title, municipality_info["url"])

                col_up, _ = st.columns([1, 5])
                with col_up:
                    if st.session_state.current_user is None:
                        st.caption(f"👍 {row['upvote_count']} endorsements")
                    elif has_upvoted(review_id, st.session_state.current_user):
                        st.caption(f"✅ Endorsed ({row['upvote_count']})")
                    else:
                        if st.button(f"👍 Endorse ({row['upvote_count']})", key=f"rev_upvote_{review_id}"):
                            if upvote_review(review_id, st.session_state.current_user):
                                add_civic_points(st.session_state.current_user, 1)
                            st.rerun()

                if row["status"] == "Open":
                    st.markdown("---")
                    st.markdown("##### 🛠️ Confirm Fixed (Upload After-Repair Proof)")
                    if isinstance(row["photo_path"], str):
                        safe_image(row["photo_path"], caption="Before Repair", width=220)
                    if st.session_state.current_user:
                        fixed_photo = st.file_uploader(
                            "Upload After-Repair Verification Photo", type=["jpg", "jpeg", "png"],
                            key=f"fixed_photo_upload_{review_id}",
                        )
                        new_rating = st.slider("New Road Quality Rating", 1, 5, 5, key=f"fixed_rating_{review_id}")
                        if st.button("✅ Confirm Fixed & Earn +15 pts", key=f"confirm_fixed_{review_id}"):
                            fixed_path = save_photo(fixed_photo.getvalue(), f"fixed_{review_id}") if fixed_photo else None
                            mark_review_fixed(review_id, new_rating, fixed_path)
                            add_civic_points(st.session_state.current_user, 15)
                            st.rerun()
                else:
                    col_b, col_a = st.columns(2)
                    with col_b:
                        if isinstance(row["photo_path"], str):
                            safe_image(row["photo_path"], caption="Before")
                    with col_a:
                        if isinstance(row["fixed_photo_path"], str):
                            safe_image(row["fixed_photo_path"], caption="After Repair (Fixed)")

                st.markdown("---")
                st.markdown("##### 💬 Spider-Chat & Notes")
                replies = fetch_replies(review_id)
                if replies.empty:
                    st.caption("No replies yet on this hazard.")
                else:
                    for _, reply in replies.iterrows():
                        st.markdown(f"**🕷️ {reply['username']}**: {reply['reply_text']}")
                        st.caption(reply["timestamp"])

                if st.session_state.current_user:
                    with st.form(f"reply_form_{review_id}", clear_on_submit=True):
                        reply_text = st.text_input("Add a comment / update", key=f"reply_input_{review_id}")
                        if st.form_submit_button("Post Reply (+2 pts)"):
                            if reply_text.strip():
                                insert_reply(review_id, st.session_state.current_user, reply_text.strip())
                                add_civic_points(st.session_state.current_user, 2)
                                st.rerun()

# ----- PAGE: NEWS FEED -------
elif nav_page == "News Feed":
    FALLBACK_HEADLINES = [
        "GCC Sanctions ₹42 Crore for Pothole Repair Blitz Across 15 Zones",
        "Monsoon Waterlogging Complaints Surge 30% in North Chennai",
        "Traffic Police Flag 12 Accident-Prone Junctions for Urgent Resurfacing",
        "Citizen Reporting App Cuts Average Pothole Repair Time in Half, Officials Say",
        "Corporation Launches Night-Shift Road Repair Crews Ahead of Festival Season",
        "Tamil Nadu Announces ₹500 Cr Smart Roads Initiative for Urban Corridors",
        "Residents' Welfare Associations Demand Faster Grievance Redressal Timelines",
        "Weather Dept Predicts Heavy Rain, Warns of Fresh Waterlogging Hotspots",
    ]
    _ticker_source_df = fetch_news_for_query(st.session_state.get("news_search_query", "Chennai road repair OR pothole"))
    if not _ticker_source_df.empty:
        _ticker_headlines = list(_ticker_source_df["title"].head(10)) + FALLBACK_HEADLINES[:4]
    else:
        _ticker_headlines = FALLBACK_HEADLINES
    _ticker_html = '<span class="hl-sep">◆</span>'.join(_ticker_headlines)

    st.markdown(
        f"""
        <div class="news-ticker-bar">
            <div class="news-ticker-tag">🔴 BREAKING</div>
            <div class="news-ticker-viewport">
                <div class="news-ticker-track">
                    <span>{_ticker_html}</span><span class="hl-sep">◆</span><span>{_ticker_html}</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="bugle-masthead">
            <div class="bugle-tagline">SERVING THE NEIGHBORHOOD SINCE DAY ONE</div>
            <h1 class="bugle-title">THE DAILY BUGLE</h1>
            <div class="bugle-tagline">REAL NEWS. REAL ROADS. REAL CITIZENS.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    news_query = st.text_input("Topic Search", value="Chennai road repair OR pothole", key="news_search_query")
    if st.button("🔄 Fetch Latest Bugle Headlines"):
        fetched = fetch_news_rss(news_query.strip())
        if fetched:
            store_news_items(fetched, news_query.strip())
            st.session_state.news_page = 1
            st.success(f"Fetched {len(fetched)} articles.")
        else:
            st.warning("No articles found for this topic.")
        st.rerun()

    if "news_page" not in st.session_state:
        st.session_state.news_page = 1
    if "_last_shown_news_query" not in st.session_state:
        st.session_state._last_shown_news_query = None
    if st.session_state._last_shown_news_query != news_query.strip():
        st.session_state.news_page = 1
        st.session_state._last_shown_news_query = news_query.strip()

    PAGE_SIZE = 8
    all_news = fetch_news_for_query(news_query.strip())

    if all_news.empty:
        st.info("No Bugle dispatches on record. Click 'Fetch Latest Bugle Headlines' above!")
    else:
        total_pages = max(1, (len(all_news) + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.news_page = min(st.session_state.news_page, total_pages)
        page = st.session_state.news_page

        start = (page - 1) * PAGE_SIZE
        page_items = all_news.iloc[start:start + PAGE_SIZE]

        for _, item in page_items.iterrows():
            news_id = int(item["id"])
            st.markdown(
                f"""
                <div class="news-card">
                    <div class="news-title">{item["title"]}</div>
                    <div class="news-meta">Source: <b>{item["source"]}</b> · {item["published"]}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            col_link, col_vote = st.columns([4, 1])
            with col_link:
                st.link_button("Read Article ↗", item["link"])
            with col_vote:
                if st.session_state.current_user is None:
                    st.caption(f"👍 {item['upvote_count']}")
                elif has_upvoted_news(news_id, st.session_state.current_user):
                    st.caption(f"✅ Endorsed ({item['upvote_count']})")
                else:
                    if st.button(f"👍 Endorse ({item['upvote_count']})", key=f"news_upvote_{news_id}"):
                        if upvote_news(news_id, st.session_state.current_user):
                            add_civic_points(st.session_state.current_user, 1)
                        st.rerun()

        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("⬅ Previous", disabled=(page <= 1)):
                st.session_state.news_page -= 1
                st.rerun()
        with col_info:
            st.markdown(f"<div style='text-align:center; padding-top:6px;'>Page {page} of {total_pages} ({len(all_news)} articles)</div>", unsafe_allow_html=True)
        with col_next:
            if st.button("Next ➡", disabled=(page >= total_pages)):
                st.session_state.news_page += 1
                st.rerun()

# ----- PAGE: PROFILE (Instagram-style) -------
elif nav_page == "Profile":
    if st.session_state.current_user is None:
        st.markdown('<div class="hero-header">', unsafe_allow_html=True)
        st.info("🕸️ Sign in from the sidebar to see your Spidey profile.")
        st.markdown('</div>', unsafe_allow_html=True)
    else:
        username = st.session_state.current_user
        score = get_civic_score(username)
        all_reviews_df = fetch_all_reviews()
        my_reviews_df = all_reviews_df[all_reviews_df["username"] == username].sort_values(
            "timestamp", ascending=False
        ) if not all_reviews_df.empty else all_reviews_df

        reports_count = len(my_reviews_df)
        fixed_count = int((my_reviews_df["status"] != "Open").sum()) if not my_reviews_df.empty else 0
        open_count = reports_count - fixed_count

        st.markdown(
            f"""
            <div class="ig-profile-header">
                <div class="ig-avatar">{username[:1].upper()}</div>
                <div>
                    <div class="ig-profile-name">🕷️ {username}</div>
                    <div class="ig-profile-badge">{civic_badge(score)}</div>
                    <div class="ig-stats-row">
                        <div><span class="ig-stat-num">{reports_count}</span><br><span class="ig-stat-lbl">Reports</span></div>
                        <div><span class="ig-stat-num">{fixed_count}</span><br><span class="ig-stat-lbl">Fixed</span></div>
                        <div><span class="ig-stat-num">{open_count}</span><br><span class="ig-stat-lbl">Open</span></div>
                        <div><span class="ig-stat-num">{score}</span><br><span class="ig-stat-lbl">Civic Score</span></div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.button("Log Out", key="profile_logout_btn"):
            st.session_state.current_user = None
            st.rerun()

        st.markdown("---")
        st.markdown("##### 🕸️ Issues You've Reported")

        if my_reviews_df.empty:
            st.info("You haven't reported any hazards yet. Head to **🧭 Map** to sling your first report!")
        else:
            cols = st.columns(3)
            for idx, (_, row) in enumerate(my_reviews_df.iterrows()):
                pill_class = "spidey-pill-open" if row["status"] == "Open" else "spidey-pill-fixed"
                with cols[idx % 3]:
                    st.markdown('<div class="ig-post-card">', unsafe_allow_html=True)
                    if row["photo_path"] and isinstance(row["photo_path"], str):
                        safe_image(row["photo_path"], use_container_width=True)
                    st.markdown(
                        f"""
                        <div class="ig-post-meta">
                            <div class="ig-post-loc">{row['category']} · {'⭐' * int(row['rating'])}</div>
                            <div class="ig-post-loc" style="font-weight:500; color:#CBD5E1;">{row['location_name'][:60]}</div>
                            <div class="ig-post-date">{row['timestamp']}</div>
                            <span class="spidey-pill {pill_class}" style="margin-top:6px; display:inline-block;">{row['status']}</span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    st.markdown('</div>', unsafe_allow_html=True)

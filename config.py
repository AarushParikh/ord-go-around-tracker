"""
config.py — Shared settings for every script in the project.

Keeping constants in one place means collector.py, detect.py, weather.py,
classify.py and app.py all agree on file paths, airport facts and labels.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"

# The real database produced by the pipeline (detect -> weather -> classify).
TRACKER_DB = DATA_DIR / "tracker.db"

# Fake database produced by make_demo_data.py so the dashboard can be built
# and shown before real data exists. The dashboard labels it loudly.
DEMO_DB = DATA_DIR / "demo.db"

# ---------------------------------------------------------------------------
# Airport facts: Chicago O'Hare (KORD)
# ---------------------------------------------------------------------------
AIRPORT_ICAO = "KORD"
AIRPORT_LAT = 41.9786
AIRPORT_LON = -87.9048
AIRPORT_ELEV_FT = 680  # field elevation, used to turn altitude (MSL) into height above the airport

# Times are stored in UTC everywhere and only converted for display.
LOCAL_TZ = ZoneInfo("America/Chicago")

# ORD's active runways, each listed with its opposite end.
# Landing on 27s/28s = "west flow"; landing on 9s/10s = "east flow".
RUNWAYS = [
    "04L", "22R", "04R", "22L",
    "09L", "27R", "09C", "27C", "09R", "27L",
    "10L", "28R", "10C", "28C", "10R", "28L",
]


def runway_heading_deg(runway: str) -> int:
    """Approximate magnetic heading from the runway number ('27R' -> 270)."""
    return int(runway[:2]) * 10


def airport_flow(runway: str) -> str:
    """Classify a landing runway into ORD's two main traffic flows."""
    heading = runway_heading_deg(runway)
    if 45 <= heading <= 135:
        return "East flow"
    if 225 <= heading <= 315:
        return "West flow"
    return "Diagonal (4/22)"


# ---------------------------------------------------------------------------
# Contributing-factor categories (must match classify.py)
# ---------------------------------------------------------------------------
FACTOR_LABELS = {
    "windshear_reported": "Windshear reported",
    "runway_occupied": "Runway occupied",
    "insufficient_spacing": "Insufficient spacing",
    "gusty_wind_or_crosswind": "Gusty wind / crosswind",
    "low_visibility_or_ceiling": "Low visibility / ceiling",
    "high_energy_approach": "High-energy approach",
    "insufficient_data": "Insufficient data",
}

CONFIDENCE_LEVELS = ["high", "medium", "low"]

# Shown in the dashboard wherever a factor appears, so nobody mistakes it
# for an official cause.
FACTOR_COLUMN_TITLE = "Likely contributing factor (rule-inferred)"

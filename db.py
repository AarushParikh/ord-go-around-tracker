"""
db.py — The database layout and the functions that read it.

Every later script WRITES into these tables and app.py READS from them.
Agreeing on this layout now is what lets us build the dashboard first.

Tables
------
meta             key/value facts, e.g. when collection started, demo or real
events           one row per detected go-around (written by detect.py)
event_tracks     the position points of each go-around flight, for the maps
event_weather    the METAR in effect at each go-around (written by weather.py)
classifications  rule-inferred factor per event (written by classify.py)
arrivals_hourly  how many aircraft landed, per hour / runway / airline
                 (written by detect.py) — the denominator for go-around RATES
"""

import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,  -- e.g. 'a1b2c3_2026-11-20T14:03:10Z'
    icao24              TEXT,              -- aircraft transponder address
    callsign            TEXT,
    airline_icao        TEXT,              -- 'UAL', 'SKW', ... (NULL if not an airline)
    go_around_time_utc  TEXT,              -- ISO 8601, UTC
    runway              TEXT,              -- runway of the aborted approach, e.g. '28C'
    ga_alt_ft           REAL,              -- barometric altitude (MSL) at the go-around point
    ga_groundspeed_kt   REAL,
    landing_time_utc    TEXT,              -- eventual landing (NULL if not seen)
    minutes_to_landing  REAL,
    landing_runway      TEXT,
    prev_callsign       TEXT,              -- the arrival ahead on the same runway
    prev_spacing_nm     REAL,
    prev_spacing_s      REAL,
    prev_on_runway      INTEGER,           -- 1 yes, 0 no, NULL unknown
    aircraft_type       TEXT,              -- phase 2 (OpenSky aircraft database)
    verified            INTEGER            -- manual check: 1 real, 0 false alarm, NULL unchecked
);

CREATE TABLE IF NOT EXISTS event_tracks (
    event_id  TEXT,
    seq       INTEGER,                     -- point order within the track
    time_utc  TEXT,
    lat       REAL,
    lon       REAL,
    alt_ft    REAL,
    PRIMARY KEY (event_id, seq)
);

CREATE TABLE IF NOT EXISTS event_weather (
    event_id      TEXT PRIMARY KEY,
    metar_time_utc TEXT,
    metar_raw     TEXT,
    wind_dir_deg  REAL,                    -- NULL when variable or calm
    wind_kt       REAL,
    gust_kt       REAL,
    visibility_sm REAL,
    ceiling_ft    REAL,                    -- lowest broken/overcast layer, AGL; NULL = none
    wx_codes      TEXT,                    -- e.g. '-RA BR'
    llws          INTEGER,                 -- 1 if low-level windshear was reported
    altimeter_inhg REAL                    -- altimeter setting, used to correct ADS-B altitudes
);

CREATE TABLE IF NOT EXISTS classifications (
    event_id          TEXT PRIMARY KEY,
    likely_factor     TEXT,
    secondary_factor  TEXT,
    confidence        TEXT,
    evidence          TEXT,
    rules_version     TEXT
);

CREATE TABLE IF NOT EXISTS arrivals_hourly (
    hour_utc      TEXT,                    -- start of the hour, ISO 8601 UTC
    runway        TEXT,
    airline_icao  TEXT,                    -- '' for non-airline traffic
    arrivals      INTEGER,
    PRIMARY KEY (hour_utc, runway, airline_icao)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the database and create any missing tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    # Databases created before a column was added get it here.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_weather)")}
    if "altimeter_inhg" not in cols:
        conn.execute("ALTER TABLE event_weather ADD COLUMN altimeter_inhg REAL")
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ---------------------------------------------------------------------------
# Read functions used by the dashboard
# ---------------------------------------------------------------------------

def load_meta(path: Path) -> dict:
    with connect(path) as conn:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return dict(rows)


def load_events(path: Path) -> pd.DataFrame:
    """One row per go-around, joined with its weather and classification."""
    query = """
        SELECT e.*,
               w.metar_time_utc, w.metar_raw, w.wind_dir_deg, w.wind_kt, w.gust_kt,
               w.visibility_sm, w.ceiling_ft, w.wx_codes, w.llws, w.altimeter_inhg,
               c.likely_factor, c.secondary_factor, c.confidence, c.evidence
        FROM events e
        LEFT JOIN event_weather   w USING (event_id)
        LEFT JOIN classifications c USING (event_id)
    """
    with connect(path) as conn:
        df = pd.read_sql_query(query, conn)
    for col in ["go_around_time_utc", "landing_time_utc", "metar_time_utc"]:
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    # ADS-B altitude is pressure altitude (altimeter at 29.92). Correct it with the
    # day's altimeter setting when we have one: ~1,000 ft per inch of mercury.
    correction = (df["altimeter_inhg"] - 29.92) * 1000
    df["ga_alt_true_ft"] = df["ga_alt_ft"] + correction.fillna(0)
    df["ga_alt_corrected"] = df["altimeter_inhg"].notna()
    return df


def load_arrivals(path: Path) -> pd.DataFrame:
    with connect(path) as conn:
        df = pd.read_sql_query("SELECT * FROM arrivals_hourly", conn)
    df["hour_utc"] = pd.to_datetime(df["hour_utc"], utc=True, format="ISO8601")
    return df


def load_tracks(path: Path) -> pd.DataFrame:
    with connect(path) as conn:
        df = pd.read_sql_query("SELECT * FROM event_tracks ORDER BY event_id, seq", conn)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True, format="ISO8601")
    return df

"""
collector.py — Polls the OpenSky Network every 30 s for aircraft around O'Hare
and saves every position report to SQLite (data/raw.db).

Designed to run unattended for weeks on the cloud VM (see deploy/README_VM.md).
Uses only Python's standard library, so the VM needs no extra installs.

How it works, in one loop:
  1. Make sure we have a valid OpenSky access token (they expire, so we refresh).
  2. Ask OpenSky for every aircraft inside the ORD box.
  3. Save each aircraft's report. If an aircraft hasn't sent a new position
     since the last poll, OpenSky repeats the old one — we skip duplicates.
  4. Sleep until the next poll. On errors, wait longer each time (backoff).

Usage:
  python3 collector.py                  (poll every 30 s, forever)
  python3 collector.py --interval 15    (once you have 8,000 feeder credits)
  python3 collector.py --once           (single poll, for testing)
"""

import argparse
import json
import logging
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RAW_DB = PROJECT_DIR / "data" / "raw.db"
CREDENTIALS = PROJECT_DIR / "credentials.json"
LOG_FILE = PROJECT_DIR / "logs" / "collector.log"

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"

# Box around O'Hare. Small boxes cost OpenSky the fewest credits per call.
ORD_BOX = {"lamin": 41.80, "lomin": -88.10, "lamax": 42.10, "lomax": -87.70}

MAX_BACKOFF_S = 15 * 60  # never wait more than 15 minutes between retries

log = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Units are stored exactly as OpenSky sends them (metres, m/s, degrees).
# detect.py converts to feet and knots later. Keeping raw units here means
# the raw data is never altered.
SCHEMA = """
CREATE TABLE IF NOT EXISTS states (
    icao24          TEXT    NOT NULL,  -- aircraft transponder address
    time_position   INTEGER NOT NULL,  -- unix time of this position report
    poll_time       INTEGER NOT NULL,  -- unix time OpenSky answered our poll
    callsign        TEXT,
    lat             REAL,
    lon             REAL,
    baro_altitude_m REAL,
    geo_altitude_m  REAL,
    velocity_ms     REAL,              -- groundspeed, metres per second
    true_track_deg  REAL,              -- direction of travel (needed to find runway alignment)
    vertical_rate_ms REAL,
    on_ground       INTEGER,           -- 1 if the aircraft reports being on the ground
    squawk          TEXT,
    source          TEXT NOT NULL DEFAULT 'opensky',  -- 'opensky' or 'receiver'
    PRIMARY KEY (icao24, time_position, source)
);

-- Lets detect.py load one day without reading the whole table.
CREATE INDEX IF NOT EXISTS idx_states_time ON states (time_position);

CREATE TABLE IF NOT EXISTS collector_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")  # safer if the VM loses power mid-write
    conn.executescript(SCHEMA)
    # Remember the very first start time: the dashboard's "Collecting since".
    conn.execute("INSERT OR IGNORE INTO collector_meta VALUES ('first_start_utc', strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO collector_meta VALUES (?, ?)", (key, str(value)))


def save_states(conn: sqlite3.Connection, data: dict) -> int:
    """Insert one poll's aircraft. Returns how many NEW position reports were saved."""
    poll_time = data.get("time")
    rows = []
    for s in data.get("states") or []:
        # OpenSky state vector fields, by position in the list:
        # 0 icao24, 1 callsign, 3 time_position, 5 lon, 6 lat, 7 baro_alt,
        # 8 on_ground, 9 velocity, 10 true_track, 11 vertical_rate,
        # 13 geo_alt, 14 squawk
        if s[3] is None or s[5] is None or s[6] is None:
            continue  # no position in this report; nothing useful to store
        rows.append((
            s[0], s[3], poll_time, (s[1] or "").strip() or None,
            s[6], s[5], s[7], s[13], s[9], s[10], s[11],
            1 if s[8] else 0, s[14],
        ))
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO states
           (icao24, time_position, poll_time, callsign, lat, lon, baro_altitude_m,
            geo_altitude_m, velocity_ms, true_track_deg, vertical_rate_ms, on_ground, squawk)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


# ---------------------------------------------------------------------------
# OpenSky API
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    """OpenSky said 'too many requests'. Carries how long to wait."""
    def __init__(self, wait_s: int):
        super().__init__(f"rate limited, retry after {wait_s} s")
        self.wait_s = wait_s


class TokenManager:
    """Gets an OAuth2 access token and refreshes it shortly before it expires."""

    def __init__(self, credentials_path: Path):
        creds = json.loads(credentials_path.read_text())
        self.client_id = creds["clientId"]
        self.client_secret = creds["clientSecret"]
        self.token = None
        self.expires_at = 0.0

    def get(self) -> str:
        if self.token is None or time.time() > self.expires_at - 60:
            self.refresh()
        return self.token

    def refresh(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as r:
            reply = json.load(r)
        self.token = reply["access_token"]
        self.expires_at = time.time() + reply.get("expires_in", 1800)
        log.info("Got new access token (valid %d min)", reply.get("expires_in", 1800) // 60)

    def invalidate(self) -> None:
        self.token = None


def fetch_states(tokens: TokenManager) -> tuple[dict, str | None]:
    """One API call. Returns (data, credits_remaining)."""
    url = STATES_URL + "?" + urllib.parse.urlencode(ORD_BOX)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tokens.get()}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), r.headers.get("X-Rate-Limit-Remaining")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(int(e.headers.get("X-Rate-Limit-Retry-After-Seconds", 300)))
        if e.code == 401:
            tokens.invalidate()  # token expired or revoked: get a fresh one next time
        raise


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    # Rotating file: max 5 MB x 5 files, so logs can never fill the disk.
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(console)
    log.setLevel(logging.INFO)


stop_requested = False


def request_stop(signum, frame):
    """systemd sends SIGTERM on 'stop'; finish the current poll and exit cleanly."""
    global stop_requested
    stop_requested = True
    log.info("Stop requested (signal %d)", signum)


def run(interval: int, once: bool, fetch=fetch_states, sleep=time.sleep) -> None:
    conn = open_db(RAW_DB)
    tokens = TokenManager(CREDENTIALS)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    failures = 0
    polls = 0
    log.info("Collector started: every %d s, box %s, saving to %s", interval, ORD_BOX, RAW_DB)

    while not stop_requested:
        started = time.time()
        try:
            data, remaining = fetch(tokens)
            new_rows = save_states(conn, data)
            polls += 1
            failures = 0
            set_meta(conn, "last_success_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            set_meta(conn, "credits_remaining", remaining)
            conn.commit()
            # Log a short summary every 10 polls (~5 min) so the log stays readable.
            if polls % 10 == 1 or once:
                log.info("Poll OK: %d aircraft, %d new positions, credits left: %s",
                         len(data.get("states") or []), new_rows, remaining)
            wait = interval
        except RateLimited as e:
            log.warning("Out of credits: waiting %d s (about %.1f h)", e.wait_s, e.wait_s / 3600)
            wait = e.wait_s
        except Exception as e:  # network blip, OpenSky down, bad reply...
            failures += 1
            wait = min(interval * 2 ** failures, MAX_BACKOFF_S)
            log.error("Poll failed (%d in a row): %s. Retrying in %d s", failures, e, wait)

        if once:
            break
        # Sleep in 1-second steps so a stop request is handled quickly.
        # Measuring from 'started' keeps polls on a steady 30 s rhythm.
        deadline = started + wait
        while not stop_requested and time.time() < deadline:
            sleep(min(1, deadline - time.time()))

    conn.close()
    log.info("Collector stopped after %d successful polls", polls)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll OpenSky for aircraft around ORD.")
    parser.add_argument("--interval", type=int, default=30,
                        help="seconds between polls (default 30; 15 once you're an active feeder)")
    parser.add_argument("--once", action="store_true", help="poll once and exit (for testing)")
    args = parser.parse_args()
    setup_logging()
    run(args.interval, args.once)

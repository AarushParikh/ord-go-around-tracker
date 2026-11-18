"""
fetch_adsblol.py — Download a day of flight history from adsb.lol's free
public archive and keep only the aircraft positions around O'Hare.

Why this exists: OpenSky blocks requests from cloud servers, but adsb.lol
publishes every aircraft track it saw each day on GitHub, openly licensed
(ODbL 1.0). Its Chicago coverage is dense: near ORD, points arrive about once
per second, versus every 30 s when polling OpenSky.

How it works for one day:
  1. Stream that day's archive (~3.5 GB, split into parts) straight from
     GitHub. Nothing big is saved to disk.
  2. The archive holds one file per aircraft. For each, keep only the points
     inside the ORD box and below 15,000 ft (or on the ground).
  3. Save those points to data/raw.db (same `states` table as collector.py,
     tagged source='adsblol'), then record the day as done.

Usage:
  python3 fetch_adsblol.py --date 2026-11-16      one specific day
  python3 fetch_adsblol.py --catch-up            every missing day up to yesterday
  python3 fetch_adsblol.py --catch-up --since 2026-09-01   backfill from a start date

Data: adsb.lol (https://adsb.lol), Open Database License (ODbL) 1.0.
"""

import argparse
import fcntl
import gzip
import json
import logging
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from collector import LOG_FILE, RAW_DB, open_db, set_meta

ARCHIVE = "https://github.com/adsblol/globe_history_{year}/releases/download/{tag}/{tag}.tar.{part}"
# The archive is published by several servers; try the main one first.
INSTANCES = ["prod-0", "prod-1", "staging-0"]

BOX = {"lamin": 41.80, "lomin": -88.10, "lamax": 42.10, "lomax": -87.70}
MAX_ALT_FT = 15_000  # higher than this is cruising traffic, irrelevant for go-arounds

FT_TO_M = 0.3048
KT_TO_MS = 0.514444
FPM_TO_MS = 0.00508

log = logging.getLogger("fetch_adsblol")


class NotPublishedYet(Exception):
    pass


# ---------------------------------------------------------------------------
# Streaming download: read the parts .aa, .ab, ... back to back as one file
# ---------------------------------------------------------------------------

def part_names():
    """'aa', 'ab', ... 'az', 'ba', ... (the suffixes the 'split' tool uses)."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    for first in letters:
        for second in letters:
            yield first + second


class ArchiveStream:
    """A file-like object that reads each archive part in turn, so Python's
    tarfile module sees one continuous file without anything saved to disk."""

    def __init__(self, url_for_part):
        self.url_for_part = url_for_part
        self.parts = part_names()
        self.current = None
        self.finished = False
        self.bytes_read = 0
        self._open_next()

    def _open_next(self):
        name = next(self.parts)
        try:
            self.current = urllib.request.urlopen(self.url_for_part(name), timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.finished = True  # no more parts
                self.current = None
                return
            raise

    def read(self, size=-1):
        chunks = []
        while not self.finished and (size < 0 or size > 0):
            data = self.current.read(size if size > 0 else 1 << 20)
            if data:
                chunks.append(data)
                self.bytes_read += len(data)
                if size > 0:
                    size -= len(data)
            else:
                self.current.close()
                self._open_next()
        return b"".join(chunks)


def find_tag(day: date) -> str:
    """Return the release tag for this day, or raise NotPublishedYet."""
    for instance in INSTANCES:
        tag = f"v{day:%Y.%m.%d}-planes-readsb-{instance}"
        url = ARCHIVE.format(year=day.year, tag=tag, part="aa")
        try:
            req = urllib.request.Request(url, method="HEAD")
            urllib.request.urlopen(req, timeout=60).close()
            return tag
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    raise NotPublishedYet(f"No archive for {day} yet")


# ---------------------------------------------------------------------------
# Turning one aircraft's trace into database rows
# ---------------------------------------------------------------------------

def trace_rows(d: dict) -> list[tuple]:
    """Rows for the points of one aircraft trace that fall inside the ORD box.

    Each trace point is a list:
      [0] seconds after the file's base timestamp, [1] lat, [2] lon,
      [3] altitude in ft or "ground", [4] groundspeed kt, [5] track deg,
      [6] flags, [7] vertical rate ft/min, [8] extra details (callsign,
      squawk...) or None, [9] source type, [10] GPS altitude ft, ...
    """
    rows = []
    callsign = squawk = None
    base = d["timestamp"]
    for p in d["trace"]:
        details = p[8] if len(p) > 8 else None
        if details:  # callsign/squawk are only sent now and then, so carry them forward
            callsign = (details.get("flight") or "").strip() or callsign
            squawk = details.get("squawk") or squawk
        lat, lon, alt = p[1], p[2], p[3]
        if not (BOX["lamin"] <= lat <= BOX["lamax"] and BOX["lomin"] <= lon <= BOX["lomax"]):
            continue
        on_ground = alt == "ground"
        if not on_ground and (alt is None or alt > MAX_ALT_FT):
            continue
        t = int(base + p[0])
        geo_alt = p[10] if len(p) > 10 else None
        rows.append((
            d["icao"], t, t, callsign, lat, lon,
            0.0 if on_ground else alt * FT_TO_M,
            geo_alt * FT_TO_M if isinstance(geo_alt, (int, float)) else None,
            p[4] * KT_TO_MS if p[4] is not None else None,
            p[5],
            p[7] * FPM_TO_MS if p[7] is not None else None,
            1 if on_ground else 0,
            squawk,
        ))
    return rows


def fetch_day(day: date, conn) -> dict:
    tag = find_tag(day)
    log.info("Fetching %s (%s)", day, tag)
    stream = ArchiveStream(lambda part: ARCHIVE.format(year=day.year, tag=tag, part=part))
    started = time.time()
    n_files = n_aircraft = n_rows = 0
    before = conn.total_changes
    # Clear any half-finished earlier attempt for this day, so reruns are clean.
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    conn.execute("DELETE FROM states WHERE source='adsblol' AND time_position >= ? AND time_position < ?",
                 (day_start, day_start + 86400))

    with tarfile.open(fileobj=stream, mode="r|") as tf:
        for member in tf:
            if not (member.isfile() and "trace_full" in member.name):
                continue
            n_files += 1
            raw = tf.extractfile(member).read()
            try:
                trace = json.loads(gzip.decompress(raw))
            except OSError:  # some files aren't compressed
                trace = json.loads(raw)
            rows = trace_rows(trace)
            if rows:
                n_aircraft += 1
                n_rows += len(rows)
                conn.executemany(
                    """INSERT OR IGNORE INTO states
                       (icao24, time_position, poll_time, callsign, lat, lon, baro_altitude_m,
                        geo_altitude_m, velocity_ms, true_track_deg, vertical_rate_ms, on_ground,
                        squawk, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'adsblol')""",
                    rows,
                )
            if n_files % 10_000 == 0:
                conn.commit()
                log.info("  ...%d aircraft files read (%.1f GB)", n_files, stream.bytes_read / 1e9)

    saved = conn.total_changes - before
    set_meta(conn, f"adsblol_done_{day}", tag)
    conn.commit()
    summary = {"day": str(day), "files": n_files, "aircraft_near_ord": n_aircraft,
               "points_saved": saved, "gb": round(stream.bytes_read / 1e9, 2),
               "minutes": round((time.time() - started) / 60, 1)}
    log.info("Done %s: %s", day, summary)
    return summary


def days_to_fetch(conn, since: date) -> list[date]:
    done = {row[0].removeprefix("adsblol_done_")
            for row in conn.execute("SELECT key FROM collector_meta WHERE key LIKE 'adsblol_done_%'")}
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    out, d = [], since
    while d <= yesterday:
        if str(d) not in done:
            out.append(d)
        d += timedelta(days=1)
    return out


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.FileHandler(LOG_FILE.with_name("fetch_adsblol.log")), logging.StreamHandler()):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def main():
    parser = argparse.ArgumentParser(description="Fetch ORD-area tracks from the adsb.lol daily archive.")
    parser.add_argument("--date", type=date.fromisoformat, help="fetch this one day (YYYY-MM-DD)")
    parser.add_argument("--catch-up", action="store_true", help="fetch every missing day up to yesterday")
    parser.add_argument("--since", type=date.fromisoformat,
                        help="first day for --catch-up (default: 7 days ago, or as saved from a previous run)")
    args = parser.parse_args()
    setup_logging()

    # Only one copy may run at a time (e.g. a long backfill and the daily timer).
    RAW_DB.parent.mkdir(parents=True, exist_ok=True)
    lock = open(RAW_DB.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another fetch is already running; exiting.")
        return

    conn = open_db(RAW_DB)

    if args.date:
        fetch_day(args.date, conn)
        return

    if not args.catch_up:
        parser.error("use --date or --catch-up")

    # Remember the backfill start date so the daily timer keeps using it.
    if args.since:
        set_meta(conn, "adsblol_since", args.since)
        conn.commit()
    saved = dict(conn.execute("SELECT key, value FROM collector_meta")).get("adsblol_since")
    since = date.fromisoformat(saved) if saved else datetime.now(timezone.utc).date() - timedelta(days=7)

    todo = days_to_fetch(conn, since)
    log.info("Catch-up: %d day(s) to fetch since %s", len(todo), since)
    for day in todo:
        try:
            fetch_day(day, conn)
        except NotPublishedYet:
            log.info("%s not published yet; will retry on the next run", day)
        except Exception as e:  # one bad day shouldn't stop the others
            log.error("Failed on %s: %s (will retry next run)", day, e)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())

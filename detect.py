"""
detect.py — Find go-arounds at O'Hare in the raw position data, and count
arrivals so go-around RATES can be computed.

Reads   data/raw.db      (positions from fetch_adsblol.py / collector.py)
Writes  data/tracker.db  (events, event_tracks, arrivals_hourly, meta)

Steps, one day at a time:
  1. Load that day's positions and convert to the units `traffic` expects
     (feet, knots, ft/min).
  2. Split each aircraft's positions into separate flights (a gap of 20+ min
     = a new flight).
  3. For each flight, find the parts where it was lined up with an ORD runway
     (`aligned_on_ils`). One aligned approach that ends on the ground is an
     ARRIVAL. Two or more aligned approaches means it might have gone around,
     so we ask `traffic`'s built-in `go_around("KORD")` to confirm.
  4. For each go-around, measure: runway, altitude and speed at the lowest
     point of the aborted approach, time until it finally landed, and the
     arrival ahead of it on the same runway (spacing, still on the runway?).
  5. Save everything to tracker.db, which the dashboard reads.

Usage:
  python detect.py                      process every new day in raw.db
  python detect.py --redo               reprocess every day
  python detect.py --plots 30           also draw 30 unreviewed events for manual checking
  python detect.py --apply-verification data/verify/verify.csv
"""

import argparse
import fcntl
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from traffic.core import Traffic
from traffic.data import airports

import airlines
import config
import db

RAW_DB = config.DATA_DIR / "raw.db"
VERIFY_DIR = config.DATA_DIR / "verify"

M_TO_FT, MS_TO_KT, MS_TO_FPM = 1 / 0.3048, 1 / 0.514444, 1 / 0.00508

# Only aircraft that came this low near ORD can be arriving there. Filtering
# them first makes detection ~20x faster than testing every aircraft.
ARRIVAL_CHECK_MAX_ALT_FT = config.AIRPORT_ELEV_FT + 2500
ARRIVAL_CHECK_RADIUS_NM = 8

# An approach counts as a landing if the aircraft is on the ground within
# this long after the approach ends (or the approach got this low).
LANDING_MAX_ALT_FT = config.AIRPORT_ELEV_FT + 300


# ---------------------------------------------------------------------------
# Small geometry helpers
# ---------------------------------------------------------------------------

def distance_nm(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in nautical miles (haversine formula)."""
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def runway_thresholds() -> dict[str, tuple[float, float]]:
    """Runway name -> (lat, lon) of its landing threshold, from OurAirports via traffic."""
    df = airports["KORD"].runways.data
    return {row.name: (row.latitude, row.longitude) for row in df.itertuples()}


def point_at(flight_df: pd.DataFrame, when: pd.Timestamp, tolerance_s=10):
    """The position closest in time to `when`, or None if there's none within tolerance."""
    if flight_df.empty:
        return None
    idx = (flight_df["timestamp"] - when).abs().idxmin()
    row = flight_df.loc[idx]
    if abs((row["timestamp"] - when).total_seconds()) > tolerance_s:
        return None
    return row


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_positions(raw_path: Path, start: datetime, end: datetime) -> pd.DataFrame:
    """Positions between start and end, in traffic's column names and units."""
    with sqlite3.connect(raw_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM states WHERE time_position >= ? AND time_position < ?",
            conn, params=(int(start.timestamp()), int(end.timestamp())))
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(df.time_position, unit="s", utc=True),
        "icao24": df.icao24,
        "callsign": df.callsign.fillna(""),
        "latitude": df.lat,
        "longitude": df.lon,
        "altitude": df.baro_altitude_m * M_TO_FT,
        "groundspeed": df.velocity_ms * MS_TO_KT,
        "track": df.true_track_deg,
        "vertical_rate": df.vertical_rate_ms * MS_TO_FPM,
        "onground": df.on_ground.astype(bool),
    }).sort_values("timestamp")
    # The archive stores "on the ground" as altitude 0; use the field elevation instead.
    df.loc[df.onground, "altitude"] = config.AIRPORT_ELEV_FT

    # Keep only aircraft that were airborne, low, and close to ORD at some point
    # (drops ground vehicles and traffic just passing overhead).
    # Distance from ORD for every row at once (numpy), instead of row by row.
    lat1, lon1 = np.radians(df.latitude), np.radians(df.longitude)
    lat2, lon2 = math.radians(config.AIRPORT_LAT), math.radians(config.AIRPORT_LON)
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * math.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    near = 2 * 3440.065 * np.arcsin(np.sqrt(a)) < ARRIVAL_CHECK_RADIUS_NM
    candidate = (~df.onground) & near & (df.altitude < ARRIVAL_CHECK_MAX_ALT_FT)
    keep = df.loc[candidate, "icao24"].unique()
    return df[df.icao24.isin(keep)]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def landing_of(flight, approaches):
    """(runway, landing time) if the flight's last approach ended in a landing, else None."""
    last = approaches[-1]
    after = flight.data[flight.data.timestamp >= last.start]
    on_ground = after[after.onground & (after.timestamp <= last.stop + timedelta(minutes=3))]
    if not on_ground.empty:
        return last.max("ILS"), on_ground.timestamp.iloc[0]
    if last.altitude_min <= LANDING_MAX_ALT_FT:
        return last.max("ILS"), last.stop
    return None


def analyse_go_around(flight, segment, landing):
    """Measurements for one go-around segment (the aborted approach + the climb)."""
    attempts = list(segment.aligned_on_ils("KORD"))
    first = attempts[0]
    # The go-around point = lowest point between the first approach and the next one.
    window = segment.data[segment.data.timestamp < attempts[1].start] if len(attempts) > 1 else first.data
    low = window.loc[window.altitude.idxmin()]
    ga_time = low.timestamp
    event = {
        "event_id": f"{flight.icao24}_{ga_time:%Y-%m-%dT%H:%M:%SZ}",
        "icao24": flight.icao24,
        "callsign": (flight.callsign or "").strip() or None,
        "go_around_time_utc": ga_time,
        "runway": first.max("ILS"),
        "ga_alt_ft": round(float(low.altitude)),
        "ga_groundspeed_kt": round(float(low.groundspeed)) if pd.notna(low.groundspeed) else None,
        "ga_lat": float(low.latitude), "ga_lon": float(low.longitude),
        "landing_time_utc": None, "minutes_to_landing": None, "landing_runway": None,
    }
    event["airline_icao"] = airlines.airline_code(event["callsign"])
    # Steepest descent in the last 60 s before the go-around while below 1,000 ft
    # above the field (stabilized-approach criterion: no more than 1,000 ft/min).
    last_min = window[(window.timestamp >= ga_time - timedelta(seconds=60))
                      & (window.altitude < config.AIRPORT_ELEV_FT + 1000) & ~window.onground]
    sink = -last_min.vertical_rate.min() if last_min.vertical_rate.notna().any() else None
    event["max_sink_fpm"] = round(float(sink)) if sink is not None and sink > 0 else None
    if landing:
        event["landing_runway"], event["landing_time_utc"] = landing
        event["minutes_to_landing"] = round((landing[1] - ga_time).total_seconds() / 60, 1)
    return event


def add_preceding_arrival(event, arrivals, flights_by_id, thresholds, own_flight):
    """Find the arrival ahead on the same runway and measure spacing.

    prev_spacing_s  = seconds between the aircraft ahead touching down and the
                      go-around (negative = it hadn't touched down yet).
    prev_spacing_nm = how far from the runway threshold the go-around aircraft
                      was at the moment the aircraft ahead touched down.
    prev_on_runway  = 1 if the aircraft ahead touched down 0-90 s before the
                      go-around and hadn't yet slowed below 30 kt; else 0.
                      A heuristic (no runway-exit data): see README.
    """
    ga_time, rwy = event["go_around_time_utc"], event["runway"]
    candidates = [a for a in arrivals
                  if a["runway"] == rwy and a["flight_id"] != own_flight.flight_id
                  and a["landing_time"] <= ga_time + timedelta(seconds=60)]
    event.update(prev_callsign=None, prev_spacing_s=None, prev_spacing_nm=None, prev_on_runway=None)
    if not candidates:
        return
    prev = max(candidates, key=lambda a: a["landing_time"])
    if (ga_time - prev["landing_time"]).total_seconds() > 300:
        return  # nothing landed on that runway in the 5 minutes before: no "aircraft ahead"
    event["prev_callsign"] = prev["callsign"]
    event["prev_spacing_s"] = round((ga_time - prev["landing_time"]).total_seconds())

    me = point_at(own_flight.data, prev["landing_time"])
    if me is not None and rwy in thresholds:
        lat, lon = thresholds[rwy]
        event["prev_spacing_nm"] = round(distance_nm(me.latitude, me.longitude, lat, lon), 2)

    # Typical time on the runway after touchdown is under about a minute, so:
    #   touched down 0-90 s before the go-around -> still on the runway (1),
    #     unless its data shows it had already slowed below 30 kt (exited) -> 0
    #   hadn't touched down yet, or landed longer ago -> 0
    gap = event["prev_spacing_s"]
    if 0 <= gap <= 90:
        ahead = point_at(flights_by_id[prev["flight_id"]].data, ga_time)
        slowed = ahead is not None and ahead.onground and (ahead.groundspeed or 0) <= 30
        event["prev_on_runway"] = 0 if slowed else 1
    else:
        event["prev_on_runway"] = 0


def detect_day(day: date, raw_path: Path, thresholds):
    """Return (events, arrivals, tracks) for one UTC day."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    # Load 1 h either side so flights crossing midnight aren't cut in half.
    df = load_positions(raw_path, start - timedelta(hours=1), end + timedelta(hours=1))
    if df.empty:
        return [], [], []

    flights = list(Traffic(df).iterate_lazy(iterate_kw=dict(by="20 min")).assign_id().eval(max_workers=1))
    flights_by_id = {f.flight_id: f for f in flights}

    arrivals, maybe_go_arounds = [], []
    for f in flights:
        # Departures and taxiing start on the ground; arrivals (and go-arounds)
        # start in the air well above the field. Skipping the rest is ~2x faster.
        first = f.data.iloc[0]
        if first.onground or not (first.altitude > config.AIRPORT_ELEV_FT + 1500):
            continue
        # Only real approaches: alignments that begin in the air. (Rolling or taxiing
        # along a runway after landing also "aligns" with it and must be ignored.)
        approaches = [a for a in f.aligned_on_ils("KORD") if not a.data.onground.iloc[0]]
        if not approaches:
            continue
        landing = landing_of(f, approaches)
        if landing:
            arrivals.append({"flight_id": f.flight_id, "runway": landing[0], "landing_time": landing[1],
                             "callsign": (f.callsign or "").strip() or None})
        if len(approaches) >= 2:  # only these can be go-arounds; test them with traffic
            maybe_go_arounds.append((f, landing))

    events, tracks = [], []
    for f, landing in maybe_go_arounds:
        for segment in f.go_around("KORD"):
            ev = analyse_go_around(f, segment, landing)
            if not (start <= ev["go_around_time_utc"] < end):
                continue  # belongs to the neighbouring day
            add_preceding_arrival(ev, arrivals, flights_by_id, thresholds, f)
            events.append(ev)
            # Track for the maps: 10 min before the go-around until landing, ~every 5 s.
            t0 = ev["go_around_time_utc"] - timedelta(minutes=10)
            t1 = (ev["landing_time_utc"] or ev["go_around_time_utc"] + timedelta(minutes=20)) + timedelta(minutes=1)
            part = f.data[(f.data.timestamp >= t0) & (f.data.timestamp <= t1)]
            part = part.set_index("timestamp").resample("5s").first().dropna(subset=["latitude"]).reset_index()
            tracks += [(ev["event_id"], i, r.timestamp.isoformat(), r.latitude, r.longitude, r.altitude)
                       for i, r in enumerate(part.itertuples())]

    day_arrivals = [a for a in arrivals if start <= a["landing_time"] < end]
    return events, day_arrivals, tracks


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def iso(t):
    return t.isoformat() if t is not None and not pd.isna(t) else None


def save_day(conn, day, events, arrivals, tracks):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    # Events: insert or update, but NEVER overwrite your manual 'verified' column.
    for e in events:
        conn.execute("""
            INSERT INTO events (event_id, icao24, callsign, airline_icao, go_around_time_utc, runway,
                ga_alt_ft, ga_groundspeed_kt, landing_time_utc, minutes_to_landing, landing_runway,
                prev_callsign, prev_spacing_nm, prev_spacing_s, prev_on_runway, max_sink_fpm)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                callsign=excluded.callsign, airline_icao=excluded.airline_icao, runway=excluded.runway,
                ga_alt_ft=excluded.ga_alt_ft, ga_groundspeed_kt=excluded.ga_groundspeed_kt,
                landing_time_utc=excluded.landing_time_utc, minutes_to_landing=excluded.minutes_to_landing,
                landing_runway=excluded.landing_runway, prev_callsign=excluded.prev_callsign,
                prev_spacing_nm=excluded.prev_spacing_nm, prev_spacing_s=excluded.prev_spacing_s,
                prev_on_runway=excluded.prev_on_runway, max_sink_fpm=excluded.max_sink_fpm""",
            (e["event_id"], e["icao24"], e["callsign"], e["airline_icao"], iso(e["go_around_time_utc"]),
             e["runway"], e["ga_alt_ft"], e["ga_groundspeed_kt"], iso(e["landing_time_utc"]),
             e["minutes_to_landing"], e["landing_runway"], e["prev_callsign"], e["prev_spacing_nm"],
             e["prev_spacing_s"], e["prev_on_runway"], e["max_sink_fpm"]))
        conn.execute("DELETE FROM event_tracks WHERE event_id = ?", (e["event_id"],))
    conn.executemany("INSERT INTO event_tracks VALUES (?,?,?,?,?,?)", tracks)

    # Arrivals per hour / runway / airline, replacing any earlier run for this day.
    conn.execute("DELETE FROM arrivals_hourly WHERE hour_utc >= ? AND hour_utc < ?", (iso(start), iso(end)))
    counts = {}
    for a in arrivals:
        hour = a["landing_time"].floor("h")
        key = (iso(hour), a["runway"], airlines.airline_code(a["callsign"]) or "")
        counts[key] = counts.get(key, 0) + 1
    conn.executemany("INSERT INTO arrivals_hourly VALUES (?,?,?,?)", [k + (n,) for k, n in counts.items()])
    db.set_meta(conn, f"detect_done_{day}", "1")
    conn.commit()


def days_in_raw(raw_path: Path) -> list[date]:
    """Days that are complete in raw.db.

    adsb.lol days count only once fetch_adsblol.py has marked them done, so a day
    that is still downloading is never processed half-finished. (For data from
    the live OpenSky collector, every day before today counts.)
    """
    with sqlite3.connect(raw_path) as conn:
        done = [k.removeprefix("adsblol_done_") for (k,) in conn.execute(
            "SELECT key FROM collector_meta WHERE key LIKE 'adsblol_done_%'")]
        if done:
            return sorted(date.fromisoformat(d) for d in done)
        lo, hi = conn.execute("SELECT MIN(time_position), MAX(time_position) FROM states").fetchone()
    if lo is None:
        return []
    d, last = datetime.fromtimestamp(lo, timezone.utc).date(), datetime.fromtimestamp(hi, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Manual verification
# ---------------------------------------------------------------------------

def export_plots(n: int):
    """Draw unreviewed events so you can judge by eye whether each is a real go-around."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    events = db.load_events(config.TRACKER_DB)
    todo = events[events.verified.isna()].sample(min(n, events.verified.isna().sum()), random_state=1)
    tracks = db.load_tracks(config.TRACKER_DB)
    thresholds = runway_thresholds()
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)

    for e in todo.itertuples():
        t = tracks[tracks.event_id == e.event_id]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        ax1.plot(t.lon, t.lat, "-", lw=1)
        for name, (lat, lon) in thresholds.items():
            ax1.plot(lon, lat, "k.", ms=3)
            ax1.annotate(name, (lon, lat), fontsize=6)
        ga = t.iloc[(t.time_utc - e.go_around_time_utc).abs().argsort()[:1]]
        ax1.plot(ga.lon, ga.lat, "ro", label="go-around point")
        ax1.set_title(f"{e.callsign} · RWY {e.runway} · {e.go_around_time_utc:%Y-%m-%d %H:%M}Z")
        ax1.set_aspect(1 / math.cos(math.radians(config.AIRPORT_LAT)))
        ax1.legend(fontsize=8)
        minutes = (t.time_utc - e.go_around_time_utc).dt.total_seconds() / 60
        ax2.plot(minutes, t.alt_ft, "-")
        ax2.axvline(0, color="r", ls="--")
        ax2.axhline(config.AIRPORT_ELEV_FT, color="gray", lw=0.8)
        ax2.set_xlabel("minutes from go-around")
        ax2.set_ylabel("altitude (ft MSL)")
        ax2.set_title("Altitude profile: expect descent, climb, second descent")
        fig.tight_layout()
        fig.savefig(VERIFY_DIR / f"{e.event_id.replace(':', '')}.png", dpi=110)
        plt.close(fig)

    sheet = todo[["event_id", "callsign", "go_around_time_utc", "runway"]].copy()
    sheet["png"] = sheet.event_id.str.replace(":", "") + ".png"
    sheet["verified"] = ""  # fill in: 1 = real go-around, 0 = false detection
    sheet.to_csv(VERIFY_DIR / "verify.csv", index=False)
    print(f"Wrote {len(todo)} plots and {VERIFY_DIR / 'verify.csv'}. Fill the 'verified' column with 1 or 0,")
    print("then run: python detect.py --apply-verification data/verify/verify.csv")


def apply_verification(csv_path: Path):
    sheet = pd.read_csv(csv_path)
    sheet = sheet[sheet.verified.astype(str).isin(["0", "1", "0.0", "1.0"])]
    with db.connect(config.TRACKER_DB) as conn:
        for row in sheet.itertuples():
            conn.execute("UPDATE events SET verified = ? WHERE event_id = ?", (int(float(row.verified)), row.event_id))
    print(f"Saved {len(sheet)} manual checks ({int(sheet.verified.astype(float).sum())} real).")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Detect ORD go-arounds in data/raw.db.")
    parser.add_argument("--redo", action="store_true", help="reprocess days already done")
    parser.add_argument("--plots", type=int, default=0, help="draw N unreviewed events for manual checking")
    parser.add_argument("--apply-verification", type=Path, help="load your filled-in verify.csv")
    args = parser.parse_args()

    if args.apply_verification:
        apply_verification(args.apply_verification)
        return

    # Only one detection run at a time (e.g. a long backfill and the daily timer).
    # A second copy exits quietly, so the daily run moves on to weather/classify.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(config.DATA_DIR / "detect.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another detection run is in progress; skipping.")
        return

    conn = db.connect(config.TRACKER_DB)
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    thresholds = runway_thresholds()
    days = days_in_raw(RAW_DB)
    today = datetime.now(timezone.utc).date()
    for day in days:
        if day >= today or (not args.redo and f"detect_done_{day}" in meta):
            continue
        events, arrivals, tracks = detect_day(day, RAW_DB, thresholds)
        save_day(conn, day, events, arrivals, tracks)
        print(f"{day}: {len(arrivals):,} arrivals, {len(events)} go-arounds")

    if days:
        db.set_meta(conn, "is_demo", "0")
        db.set_meta(conn, "collection_start_utc", datetime(days[0].year, days[0].month, days[0].day,
                                                            tzinfo=timezone.utc).isoformat())
        db.set_meta(conn, "last_updated_utc", datetime.now(timezone.utc).isoformat())
    conn.close()

    if args.plots:
        export_plots(args.plots)


if __name__ == "__main__":
    main()

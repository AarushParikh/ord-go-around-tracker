"""
weather.py — Attach the O'Hare weather report (METAR/SPECI) that was in effect
at each go-around.

Source: Iowa Environmental Mesonet (IEM) ASOS archive, free, no account.
Reports are downloaded one month at a time and cached in data/metar/.

For each go-around we keep the LATEST report issued at or before the go-around
time (never one from after: the crew couldn't have known it), as long as it
is no more than 90 minutes old. Hourly METARs come out at about :51, and
SPECIs are issued in between whenever conditions change significantly.

Saved per event (table event_weather):
  wind direction / speed / gust, visibility, ceiling (lowest broken or
  overcast layer, or vertical visibility), weather codes (e.g. -RA BR),
  windshear reported (WS / LLWS in the report), altimeter setting.

The altimeter setting also lets us correct the go-around HEIGHT: ADS-B
reports pressure altitude (as if the altimeter were set to 29.92 inHg), so
  true altitude ≈ pressure altitude + (altimeter − 29.92) × 1000 ft.

Usage:  python weather.py          (fills in every event that has no weather yet)
        python weather.py --redo   (redoes all events)
"""

import argparse
import csv
import io
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
import db

METAR_DIR = config.DATA_DIR / "metar"
IEM_URL = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=ORD&data=all"
           "&year1={y1}&month1={m1}&day1=1&year2={y2}&month2={m2}&day2=1"
           "&tz=Etc/UTC&format=onlycomma&latlon=no&missing=empty&trace=empty"
           "&report_type=3&report_type=4")  # 3 = routine METAR, 4 = SPECI
MAX_REPORT_AGE = timedelta(minutes=90)
CEILING_COVERS = {"BKN", "OVC", "VV"}  # layers that count as a ceiling
WINDSHEAR = re.compile(r"\b(LLWS|WS)\b")


# ---------------------------------------------------------------------------
# Downloading (one month per file, cached)
# ---------------------------------------------------------------------------

def month_file(year: int, month: int):
    return METAR_DIR / f"KORD_{year}-{month:02d}.csv"


def download_month(year: int, month: int) -> None:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    url = IEM_URL.format(y1=year, m1=month, y2=nxt.year, m2=nxt.month)
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode()
    METAR_DIR.mkdir(parents=True, exist_ok=True)
    month_file(year, month).write_text(text)


def load_reports(months: set[tuple[int, int]]) -> pd.DataFrame:
    """All reports for the given (year, month)s. Past months are downloaded once;
    the current month is re-downloaded every run because it's still growing."""
    now = datetime.now(timezone.utc)
    frames = []
    for year, month in sorted(months):
        path = month_file(year, month)
        if not path.exists() or (year, month) == (now.year, now.month):
            download_month(year, month)
        frames.append(parse_iem_csv(path.read_text()))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).drop_duplicates("valid").sort_values("valid").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ceiling_ft(row: dict):
    """Height of the lowest broken/overcast layer (or vertical visibility), else None."""
    heights = [number(row.get(f"skyl{i}")) for i in range(1, 5)
               if (row.get(f"skyc{i}") or "").strip() in CEILING_COVERS]
    heights = [h for h in heights if h is not None]
    return min(heights) if heights else None


def parse_iem_csv(text: str) -> pd.DataFrame:
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if not row.get("valid"):
            continue
        metar = (row.get("metar") or "").strip()
        rows.append({
            "valid": pd.Timestamp(row["valid"], tz="UTC"),
            "metar_raw": metar,
            "wind_dir_deg": number(row.get("drct")),   # empty when calm or variable
            "wind_kt": number(row.get("sknt")),
            "gust_kt": number(row.get("gust")),
            "visibility_sm": number(row.get("vsby")),
            "ceiling_ft": ceiling_ft(row),
            "wx_codes": (row.get("wxcodes") or "").strip() or None,
            "llws": 1 if WINDSHEAR.search(metar) else 0,
            "altimeter_inhg": number(row.get("alti")),
        })
    return pd.DataFrame(rows)


def report_in_effect(reports: pd.DataFrame, when: pd.Timestamp):
    """The latest report at or before `when`, if it's recent enough."""
    earlier = reports[reports.valid <= when]
    if earlier.empty:
        return None
    latest = earlier.iloc[-1]
    return latest if when - latest.valid <= MAX_REPORT_AGE else None


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Attach METARs to go-arounds in tracker.db.")
    parser.add_argument("--redo", action="store_true", help="redo events that already have weather")
    args = parser.parse_args()

    conn = db.connect(config.TRACKER_DB)
    query = "SELECT event_id, go_around_time_utc FROM events"
    if not args.redo:
        query += " WHERE event_id NOT IN (SELECT event_id FROM event_weather)"
    events = pd.read_sql_query(query, conn)
    if events.empty:
        print("No events need weather.")
        return
    events["t"] = pd.to_datetime(events.go_around_time_utc, utc=True, format="ISO8601")

    # Months needed, plus the previous month for go-arounds early on the 1st.
    months = set()
    for t in events.t:
        months.add((t.year, t.month))
        before = t - MAX_REPORT_AGE
        months.add((before.year, before.month))
    reports = load_reports(months)

    found = 0
    for e in events.itertuples():
        r = report_in_effect(reports, e.t)
        if r is None:
            continue
        found += 1
        conn.execute(
            """INSERT OR REPLACE INTO event_weather
               (event_id, metar_time_utc, metar_raw, wind_dir_deg, wind_kt, gust_kt,
                visibility_sm, ceiling_ft, wx_codes, llws, altimeter_inhg)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (e.event_id, r.valid.isoformat(), r.metar_raw, r.wind_dir_deg, r.wind_kt, r.gust_kt,
             r.visibility_sm, r.ceiling_ft, r.wx_codes, int(r.llws), r.altimeter_inhg))
    conn.commit()
    conn.close()
    print(f"Weather attached to {found} of {len(events)} events "
          f"({len(events) - found} had no report within {MAX_REPORT_AGE.seconds // 60} min).")


if __name__ == "__main__":
    main()

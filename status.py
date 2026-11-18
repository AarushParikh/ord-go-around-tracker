"""
status.py — Health check for the collector. Run it every few days:

    python3 status.py

It answers four questions:
  * Is data still arriving?          (time of last successful poll)
  * How much was collected recently? (positions in the last hour / 24 h)
  * Am I within my OpenSky credits?  (credits left today)
  * Is the disk OK?                  (database size and free space)
"""

import shutil
import sqlite3
import time
from datetime import datetime, timezone

from collector import RAW_DB


def main():
    if not RAW_DB.exists():
        print("No database yet: the collector has never saved anything.")
        return

    conn = sqlite3.connect(RAW_DB)
    meta = dict(conn.execute("SELECT key, value FROM collector_meta"))
    now = int(time.time())
    last_hour = conn.execute("SELECT COUNT(*) FROM states WHERE poll_time > ?", (now - 3600,)).fetchone()[0]
    last_day = conn.execute("SELECT COUNT(*) FROM states WHERE poll_time > ?", (now - 86400,)).fetchone()[0]
    aircraft_day = conn.execute(
        "SELECT COUNT(DISTINCT icao24) FROM states WHERE poll_time > ?", (now - 86400,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
    conn.close()

    # adsb.lol archive days (the main data source on the VM)
    days = sorted(k.removeprefix("adsblol_done_") for k in meta if k.startswith("adsblol_done_"))
    if days:
        print(f"adsb.lol days fetched: {len(days)}  (first {days[0]}, latest {days[-1]})")
        latest = datetime.fromisoformat(days[-1]).date()
        behind = (datetime.now(timezone.utc).date() - latest).days
        print("Archive status       : " + ("OK" if behind <= 2 else f"PROBLEM: latest day is {behind} days old"))

    last = meta.get("last_success_utc")
    if last:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 60
        health = "OK" if age_min < 5 else "PROBLEM: no successful poll recently"
        print(f"Last successful poll : {last}  ({age_min:.0f} min ago)  -> {health}")
    elif not days:
        print("Last successful poll : never")

    db_mb = RAW_DB.stat().st_size / 1e6
    free_gb = shutil.disk_usage(RAW_DB.parent).free / 1e9
    print(f"Collecting since     : {meta.get('first_start_utc', '?')}")
    print(f"Positions last hour  : {last_hour:,}")
    print(f"Positions last 24 h  : {last_day:,}  ({aircraft_day:,} different aircraft)")
    print(f"Positions total      : {total:,}")
    print(f"OpenSky credits left : {meta.get('credits_remaining', '?')}")
    print(f"Database size        : {db_mb:,.1f} MB   Free disk: {free_gb:,.1f} GB")
    if free_gb < 2:
        print("WARNING: disk almost full")


if __name__ == "__main__":
    main()

"""
status.py — Health check for the collector. Run it every few days:

    python3 status.py

It answers:
  * Which archive days are fetched, and is the fetch keeping up?
  * (OpenSky collector only) when was the last successful poll?
  * Is the disk OK?  (database size and free space)

Add --full to also count all stored positions (slow on the VM).
"""

import shutil
import sys
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
    # Counting rows means reading the whole database, which is slow on the VM's
    # disk while a fetch is writing. Only do it when asked: python3 status.py --full
    full = "--full" in sys.argv
    if full:
        now = int(time.time())
        total = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
        last_day = conn.execute("SELECT COUNT(*) FROM states WHERE poll_time > ?", (now - 86400,)).fetchone()[0]
    conn.close()

    # adsb.lol archive days (the main data source on the VM)
    days = sorted(k.removeprefix("adsblol_done_") for k in meta if k.startswith("adsblol_done_"))
    if days:
        print(f"adsb.lol days fetched: {len(days)}  (first {days[0]}, latest {days[-1]})")
        latest = datetime.fromisoformat(days[-1]).date()
        behind = (datetime.now(timezone.utc).date() - latest).days
        import fcntl
        running = False
        with open(RAW_DB.with_suffix(".lock"), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
        if behind <= 2:
            print("Archive status       : OK")
        elif running:
            print(f"Archive status       : catching up (fetch running; latest day {behind} days old)")
        else:
            print(f"Archive status       : PROBLEM: latest day is {behind} days old and no fetch is running")

    last = meta.get("last_success_utc")
    if last:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 60
        health = "OK" if age_min < 5 else "PROBLEM: no successful poll recently"
        print(f"Last successful poll : {last}  ({age_min:.0f} min ago)  -> {health}")
    elif not days:
        print("Last successful poll : never")

    db_mb = RAW_DB.stat().st_size / 1e6
    free_gb = shutil.disk_usage(RAW_DB.parent).free / 1e9
    if full:
        print(f"Positions total      : {total:,}   (last 24 h: {last_day:,})")
    print(f"Database size        : {db_mb:,.1f} MB   Free disk: {free_gb:,.1f} GB")
    if free_gb < 2:
        print("WARNING: disk almost full")


if __name__ == "__main__":
    main()

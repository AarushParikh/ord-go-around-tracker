"""
make_demo_data.py — Build a FAKE database so the dashboard can be developed
and demonstrated before real data has been collected.

Everything here is randomly generated. The database is flagged with
meta.is_demo = 1, and app.py shows a large "DEMO DATA" warning whenever it
reads this file. Never present these numbers as real results.

Usage:  python make_demo_data.py            (45 days of fake data)
        python make_demo_data.py --days 90
"""

import argparse
import math
import random
from datetime import datetime, timedelta, timezone

import config
import db
import classify

random.seed(42)  # same fake data every run, so screenshots are repeatable

NM_TO_DEG_LAT = 1 / 60  # 1 nautical mile is 1/60 of a degree of latitude

# Rough share of ORD arrivals by airline code (invented for the demo).
AIRLINE_WEIGHTS = {
    "UAL": 30, "AAL": 22, "SKW": 14, "ENY": 9, "RPA": 6, "DAL": 4, "SWA": 3,
    "JIA": 3, "ASA": 2, "GJS": 2, "NKS": 1, "FFT": 1, "ACA": 1, "FDX": 1, "UPS": 1,
}

# Landing runways in each flow (ORD normally lands three parallels at once).
FLOW_RUNWAYS = {
    "west": ["27L", "28C", "27R", "28R"],
    "east": ["10C", "09R", "10L", "09L"],
}

# Weather "regimes" for a day: probability, then how they look.
WEATHER_REGIMES = [
    # name,     prob, wind_kt, gust_kt, vis_sm, ceiling_ft, wx,     llws
    ("calm",     0.45, (3, 9),   None,     10.0, None,       "",     0),
    ("windy",    0.25, (14, 22), (24, 35), 10.0, 4500,       "",     0),
    ("low_ifr",  0.15, (4, 10),  None,     0.75, 300,        "BR",   0),
    ("rain",     0.10, (8, 15),  (18, 25), 3.0,  1200,       "-RA",  0),
    ("storms",   0.05, (15, 25), (30, 45), 2.0,  2500,       "TSRA", 1),
]


def pick_weighted(weights: dict):
    return random.choices(list(weights), weights=list(weights.values()))[0]


def offset(lat, lon, north_nm, east_nm):
    """Move a point by some nautical miles north/east (flat-earth is fine at this scale)."""
    dlat = north_nm * NM_TO_DEG_LAT
    dlon = east_nm * NM_TO_DEG_LAT / math.cos(math.radians(lat))
    return lat + dlat, lon + dlon


def runway_threshold(runway):
    """Fake but plausible landing threshold: parallels spread north/south of the field."""
    lateral = {"L": 1.0, "C": 0.0, "R": -1.0}.get(runway[-1], 0.0)
    heading = math.radians(config.runway_heading_deg(runway))
    # Threshold sits ~1 nm before the field centre along the landing direction,
    # shifted sideways for left/centre/right parallels.
    north = -math.cos(heading) * 1.0 + math.sin(heading) * lateral * 0.6
    east = -math.sin(heading) * 1.0 - math.cos(heading) * lateral * 0.6
    return offset(config.AIRPORT_LAT, config.AIRPORT_LON, north, east)


def fake_track(runway, t0, ga_alt_ft):
    """Approach -> go-around -> circuit -> landing, as a list of (time, lat, lon, alt)."""
    heading = math.radians(config.runway_heading_deg(runway))
    hn, he = math.cos(heading), math.sin(heading)          # unit vector along runway
    ln, le = -he, hn                                       # unit vector to the left
    thr_lat, thr_lon = runway_threshold(runway)
    ga_dist = (ga_alt_ft - config.AIRPORT_ELEV_FT) / 318   # ~3 degree glidepath: 318 ft per nm

    # Waypoints in (nm along runway from threshold, nm to the left, altitude ft)
    waypoints = [(-10, 0, 3700), (-ga_dist, 0, ga_alt_ft), (0, 0, ga_alt_ft + 500),
                 (4, 0, 3000), (6, 3, 4000), (2, 6, 4000), (-8, 6, 4000),
                 (-12, 3, 4000), (-10, 0, 3700), (0, 0, config.AIRPORT_ELEV_FT)]
    seconds_per_nm = 25  # ~145 kt
    points, t = [], t0 - timedelta(seconds=(10 - ga_dist) * seconds_per_nm)
    for i, (along, left, alt) in enumerate(waypoints):
        if i > 0:
            prev = waypoints[i - 1]
            t += timedelta(seconds=math.dist(prev[:2], (along, left)) * seconds_per_nm)
        lat, lon = offset(thr_lat, thr_lon, along * hn + left * ln, along * he + left * le)
        points.append((t, lat, lon, alt))
    return points


def metar_text(t, wind_dir, wind_kt, gust, vis, ceiling, wx, llws):
    """Build a realistic-looking METAR string from the fake weather."""
    wind = f"{wind_dir:03d}{wind_kt:02d}" + (f"G{gust:02d}" if gust else "") + "KT"
    vis_txt = "3/4SM" if vis == 0.75 else f"{vis:g}SM"
    sky = f"OVC{ceiling // 100:03d}" if ceiling else "FEW250"
    rmk = " RMK AO2 WS ALL RWY" if llws else " RMK AO2"
    return f"METAR KORD {t:%d%H%M}Z {wind} {vis_txt} {wx + ' ' if wx else ''}{sky} 12/08 A2992{rmk}"


def main(days: int):
    config.DEMO_DB.unlink(missing_ok=True)
    conn = db.connect(config.DEMO_DB)

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    db.set_meta(conn, "is_demo", "1")
    db.set_meta(conn, "collection_start_utc", start.isoformat())
    db.set_meta(conn, "last_updated_utc", end.isoformat())

    n_events = 0
    for day in range(days):
        day_start = start + timedelta(days=day)
        flow = "west" if random.random() < 0.7 else "east"
        name, _, wind_rng, gust_rng, vis, ceiling, wx, llws_day = random.choices(
            WEATHER_REGIMES, weights=[r[1] for r in WEATHER_REGIMES])[0]
        wind_dir = random.choice([250, 270, 290]) if flow == "west" else random.choice([70, 90, 110])
        wind_kt = random.randint(*wind_rng)
        gust = random.randint(*gust_rng) if gust_rng else None

        # --- arrivals per hour (ORD is busiest from ~6am to ~10pm local) ---
        hourly = {}
        for hour in range(24):
            local_hour = (day_start + timedelta(hours=hour)).astimezone(config.LOCAL_TZ).hour
            n = random.randint(55, 85) if 6 <= local_hour <= 22 else random.randint(3, 15)
            for _ in range(n):
                key = (hour, random.choice(FLOW_RUNWAYS[flow][:3]), pick_weighted(AIRLINE_WEIGHTS))
                hourly[key] = hourly.get(key, 0) + 1
        conn.executemany(
            "INSERT INTO arrivals_hourly VALUES (?, ?, ?, ?)",
            [((day_start + timedelta(hours=h)).isoformat(), rwy, al, n)
             for (h, rwy, al), n in hourly.items()],
        )

        # --- go-arounds: more on bad-weather days ---
        expected = {"calm": 2, "windy": 5, "low_ifr": 6, "rain": 4, "storms": 9}[name]
        for _ in range(max(0, round(random.gauss(expected, 1.5)))):
            t = day_start + timedelta(hours=random.uniform(11, 28) % 24)
            airline = pick_weighted(AIRLINE_WEIGHTS)
            callsign = f"{airline}{random.randint(100, 5999)}"
            icao24 = f"{random.randrange(16**6):06x}"
            runway = random.choice(FLOW_RUNWAYS[flow][:3])
            ga_alt = random.uniform(900, 2400)
            track = fake_track(runway, t, ga_alt)
            landing_t = track[-1][0]
            spacing_nm = round(random.uniform(1.8, 6.0), 1)
            event_id = f"{icao24}_{t:%Y-%m-%dT%H:%M:%SZ}"

            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, icao24, callsign, airline, t.isoformat(), runway,
                 round(ga_alt), round(random.uniform(125, 185)), landing_t.isoformat(),
                 round((landing_t - t).total_seconds() / 60, 1), runway,
                 f"{pick_weighted(AIRLINE_WEIGHTS)}{random.randint(100, 5999)}",
                 spacing_nm, round(spacing_nm * 25),
                 1 if random.random() < 0.12 else (0 if random.random() < 0.8 else None),
                 None,
                 random.choice([1, 1, 1, 1, 0]) if random.random() < 0.4 else None),
            )
            conn.executemany(
                "INSERT INTO event_tracks VALUES (?,?,?,?,?,?)",
                [(event_id, i, pt.isoformat(), lat, lon, alt)
                 for i, (pt, lat, lon, alt) in enumerate(track)],
            )
            metar_t = t.replace(minute=51, second=0) - (timedelta(hours=1) if t.minute < 51 else timedelta())
            conn.execute(
                "INSERT INTO event_weather VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event_id, metar_t.isoformat(),
                 metar_text(metar_t, wind_dir, wind_kt, gust, vis, ceiling, wx, llws_day),
                 wind_dir, wind_kt, gust, vis, ceiling, wx, llws_day),
            )
            n_events += 1

    conn.commit()
    conn.close()
    classify.run(config.DEMO_DB)  # fill in factors using the same rules as the real pipeline
    print(f"Wrote {n_events} fake go-arounds over {days} days to {config.DEMO_DB}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45)
    main(parser.parse_args().days)

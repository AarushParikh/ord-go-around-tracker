"""
classify.py — Rule-based "likely contributing factor" for each go-around.

Plain if-statements, no AI. Each rule looks at one kind of evidence; every
rule that fires is collected, and the strongest one becomes the likely
factor. Nothing here is an official cause: ATC and the crew know the real
reason, and we only see the trajectory and the weather.

!!! DRAFT THRESHOLDS !!!
The numbers below are placeholders to make the pipeline run end to end.
They are finalized in Block 6, and every one needs a reason you can explain
in an interview (and in the README) before the results are shown publicly.

Run on the real database:   python classify.py
Run on the demo database:   python classify.py --demo
"""

import argparse
import math

import pandas as pd

import config
import db

RULES_VERSION = "draft-0.1"

# --- DRAFT thresholds (see note above) -------------------------------------
GUST_KT_MEDIUM = 25          # gusts at/above this -> medium confidence
GUST_KT_LOW = 18             # gusts at/above this -> low confidence
CROSSWIND_KT = 20            # steady crosswind component at/above this
LOW_VIS_SM = 1.0             # visibility below this (statute miles)
LOW_CEILING_FT = 500         # ceiling below this (feet above the airport)
MIN_SPACING_NM = 2.5         # spacing to the arrival ahead below this
HIGH_ENERGY_GS_KT = 170      # groundspeed above this ...
HIGH_ENERGY_MAX_AGL_FT = 1000  # ... while lower than this above the airport

# Higher number = stronger evidence. Used to pick the primary factor.
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def crosswind_component(wind_dir_deg, wind_kt, runway) -> float | None:
    """Crosswind (knots) on a runway. None if wind direction is unknown.

    Simplification to mention in the README: METAR wind is TRUE direction and
    runway numbers are MAGNETIC; at ORD the difference is only a few degrees.
    """
    if wind_dir_deg is None or wind_kt is None or pd.isna(wind_dir_deg) or pd.isna(wind_kt):
        return None
    angle = math.radians(wind_dir_deg - config.runway_heading_deg(runway))
    return abs(wind_kt * math.sin(angle))


def present(value) -> bool:
    """True if a value exists (not None / NaN)."""
    return value is not None and not pd.isna(value)


def evaluate_rules(ev: dict) -> list[tuple[str, str, str]]:
    """Return every rule that fires as (factor, confidence, evidence text)."""
    fired = []

    # 1. Windshear reported in the METAR: the most direct weather evidence.
    if ev.get("llws") == 1:
        fired.append(("windshear_reported", "high",
                      f"METAR reports low-level windshear ({ev.get('metar_raw', '')[:60]}...)."))

    # 2. Arrival ahead still on/near the runway. Capped at MEDIUM because
    #    positions are only sampled every ~30 s and ground coverage is patchy.
    if ev.get("prev_on_runway") == 1:
        fired.append(("runway_occupied", "medium",
                      f"Preceding arrival {ev.get('prev_callsign') or '(unknown)'} appears to be "
                      f"on/near runway {ev['runway']} at the go-around time."))

    # 3. Too close to the arrival ahead.
    spacing = ev.get("prev_spacing_nm")
    if present(spacing) and spacing < MIN_SPACING_NM:
        fired.append(("insufficient_spacing", "medium",
                      f"Only {spacing:.1f} nm behind {ev.get('prev_callsign') or 'the preceding arrival'} "
                      f"({ev.get('prev_spacing_s', 0):.0f} s)."))

    # 4. Gusts or crosswind.
    gust = ev.get("gust_kt")
    xwind = crosswind_component(ev.get("wind_dir_deg"), ev.get("wind_kt"), ev["runway"])
    if present(gust) and gust >= GUST_KT_MEDIUM:
        fired.append(("gusty_wind_or_crosswind", "medium", f"Gusts to {gust:.0f} kt."))
    elif xwind is not None and xwind >= CROSSWIND_KT:
        fired.append(("gusty_wind_or_crosswind", "medium",
                      f"Crosswind about {xwind:.0f} kt on runway {ev['runway']}."))
    elif present(gust) and gust >= GUST_KT_LOW:
        fired.append(("gusty_wind_or_crosswind", "low", f"Gusts to {gust:.0f} kt."))

    # 5. Low visibility or ceiling. LOW confidence: it suggests the crew may
    #    not have seen the runway at minimums, but we cannot confirm that.
    vis, ceiling = ev.get("visibility_sm"), ev.get("ceiling_ft")
    if (present(vis) and vis < LOW_VIS_SM) or (present(ceiling) and ceiling < LOW_CEILING_FT):
        vis_txt = f"{vis:g} SM" if present(vis) else "n/a"
        ceil_txt = f"{ceiling:.0f} ft" if present(ceiling) else "none"
        fired.append(("low_visibility_or_ceiling", "low",
                      f"Visibility {vis_txt}, ceiling {ceil_txt}."))

    # 6. Fast and low at the go-around point. LOW confidence: groundspeed
    #    includes wind, so a strong tailwind can look like a fast approach.
    gs, alt = ev.get("ga_groundspeed_kt"), ev.get("ga_alt_ft")
    if present(gs) and present(alt):
        agl = alt - config.AIRPORT_ELEV_FT
        if gs > HIGH_ENERGY_GS_KT and agl < HIGH_ENERGY_MAX_AGL_FT:
            fired.append(("high_energy_approach", "low",
                          f"{gs:.0f} kt groundspeed at about {agl:.0f} ft above the airport."))

    return fired


def classify_event(ev: dict) -> dict:
    """Pick the primary and secondary factor for one event."""
    fired = evaluate_rules(ev)
    if not fired:
        return {
            "likely_factor": "insufficient_data",
            "secondary_factor": None,
            "confidence": "low",
            "evidence": "No rule matched the available trajectory and weather data.",
        }
    # Sort strongest first. Python's sort is stable, so ties keep rule order
    # (the order the rules are written above = our priority order).
    fired.sort(key=lambda r: CONFIDENCE_RANK[r[1]], reverse=True)
    primary = fired[0]
    secondary = fired[1][0] if len(fired) > 1 else None
    return {
        "likely_factor": primary[0],
        "secondary_factor": secondary,
        "confidence": primary[1],
        "evidence": " ".join(r[2] for r in fired[:2]),
    }


def run(path) -> int:
    """Classify every event in the database. Returns how many were written."""
    events = db.load_events(path)
    conn = db.connect(path)
    for ev in events.to_dict("records"):
        result = classify_event(ev)
        conn.execute(
            """INSERT OR REPLACE INTO classifications
               (event_id, likely_factor, secondary_factor, confidence, evidence, rules_version)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ev["event_id"], result["likely_factor"], result["secondary_factor"],
             result["confidence"], result["evidence"], RULES_VERSION),
        )
    conn.commit()
    conn.close()
    return len(events)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--demo", action="store_true", help="classify the demo database")
    args = parser.parse_args()
    target = config.DEMO_DB if args.demo else config.TRACKER_DB
    n = run(target)
    print(f"Classified {n} events in {target.name} (rules {RULES_VERSION}).")

"""
classify.py — Rule-based "likely contributing factor" for each go-around.

Plain if-statements, no AI. Each rule looks at one kind of evidence; every
rule that fires is collected, and the strongest one becomes the likely
factor. Nothing here is an official cause: ATC and the crew know the real
reason, and we only see the trajectory and the weather.

Thresholds (version 1.0) and where each comes from:
  * Windshear: WS/LLWS in the METAR. A direct report. (US windshear alerts
    usually reach pilots via ATC/ATIS instead, so this rarely fires.)
  * Runway occupied: aircraft ahead touched down 0-90 s before the go-around
    and hadn't slowed below 30 kt (measured in detect.py). FAA's reduced
    separation on final assumes average runway occupancy of 50 s or less
    (FAA Notice JO 7210.765), so 90 s is a generous margin.
  * Insufficient spacing: under 2.5 nm behind the aircraft ahead. 2.5 nm is
    the minimum FAA allows even under reduced separation on final, within
    10 nm of the runway (FAA Notice JO 7210.765).
  * Gusty wind / crosswind: METARs report gusts only when peaks and lulls
    differ by 10 kt or more (Federal Meteorological Handbook No. 1, 5.4.4).
    Gust spread of 15+ kt or crosswind of 20+ kt -> medium; any gust -> low.
    (15 and 20 kt are this project's judgment, not regulations.)
  * Low visibility / ceiling: ceiling under 500 ft or visibility under 1 mile,
    i.e. near instrument-landing minimums. Low confidence: ORD has
    lower-minimum approaches for many aircraft.
  * High-energy (unstabilized) approach: industry criteria (Flight Safety
    Foundation) require a stabilized approach by 1,000 ft above the airport,
    with speed no more than Vref + 20 kt and descent no more than 1,000 ft/min.
    We flag: estimated airspeed (groundspeed + headwind) over 170 kt, or descent
    steeper than 1,000 ft/min, below 1,000 ft. Low confidence: we see neither
    the real airspeed nor the aircraft's Vref.

Run on the real database:   python classify.py
Run on the demo database:   python classify.py --demo
"""

import argparse
import math

import pandas as pd

import config
import db

RULES_VERSION = "1.0"

GUST_SPREAD_MEDIUM_KT = 15     # gust minus steady wind, at/above this -> medium
CROSSWIND_KT = 20              # steady crosswind component at/above this -> medium
LOW_VIS_SM = 1.0               # visibility below this (statute miles)
LOW_CEILING_FT = 500           # ceiling below this (feet above the airport)
MIN_SPACING_NM = 2.5           # spacing to the arrival ahead below this
STABILIZED_GATE_FT = 1000      # height above the field where approaches must be stable
HIGH_ENERGY_AIRSPEED_KT = 170  # estimated airspeed above this (about Vref + 20 for most airliners)
MAX_SINK_FPM = 1000            # descent rate above this

# Higher number = stronger evidence. Used to pick the primary factor.
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def wind_components(wind_dir_deg, wind_kt, runway):
    """(headwind, crosswind) in knots for a runway; None if wind is unknown/variable.

    Simplification to mention in the README: METAR wind is TRUE direction and
    runway numbers are MAGNETIC; at ORD the difference is only a few degrees.
    """
    if wind_dir_deg is None or wind_kt is None or pd.isna(wind_dir_deg) or pd.isna(wind_kt):
        return None
    angle = math.radians(wind_dir_deg - config.runway_heading_deg(runway))
    return wind_kt * math.cos(angle), abs(wind_kt * math.sin(angle))


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

    # 2. Arrival ahead still on the runway. Capped at MEDIUM: inferred from its
    #    touchdown time and speed; there is no data on when it left the runway.
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
    gust, wind = ev.get("gust_kt"), ev.get("wind_kt")
    comps = wind_components(ev.get("wind_dir_deg"), wind, ev["runway"])
    spread = gust - wind if present(gust) and present(wind) else None
    if spread is not None and spread >= GUST_SPREAD_MEDIUM_KT:
        fired.append(("gusty_wind_or_crosswind", "medium",
                      f"Wind {wind:.0f} kt gusting {gust:.0f} kt ({spread:.0f} kt spread)."))
    elif comps is not None and comps[1] >= CROSSWIND_KT:
        fired.append(("gusty_wind_or_crosswind", "medium",
                      f"Crosswind about {comps[1]:.0f} kt on runway {ev['runway']}."))
    elif present(gust):
        fired.append(("gusty_wind_or_crosswind", "low", f"Gusts to {gust:.0f} kt reported."))

    # 5. Low visibility or ceiling (near instrument minimums).
    vis, ceiling = ev.get("visibility_sm"), ev.get("ceiling_ft")
    if (present(vis) and vis < LOW_VIS_SM) or (present(ceiling) and ceiling < LOW_CEILING_FT):
        vis_txt = f"{vis:g} SM" if present(vis) else "n/a"
        ceil_txt = f"{ceiling:.0f} ft" if present(ceiling) else "none"
        fired.append(("low_visibility_or_ceiling", "low",
                      f"Visibility {vis_txt}, ceiling {ceil_txt}."))

    # 6. Unstabilized (high-energy) approach below the 1,000 ft gate.
    alt = ev.get("ga_alt_true_ft", ev.get("ga_alt_ft"))
    if present(alt) and alt - config.AIRPORT_ELEV_FT < STABILIZED_GATE_FT:
        gs, sink = ev.get("ga_groundspeed_kt"), ev.get("max_sink_fpm")
        headwind = comps[0] if comps is not None else 0.0
        airspeed = gs + headwind if present(gs) else None
        reasons = []
        if airspeed is not None and airspeed > HIGH_ENERGY_AIRSPEED_KT:
            reasons.append(f"estimated airspeed {airspeed:.0f} kt (groundspeed {gs:.0f} + headwind {headwind:.0f})")
        if present(sink) and sink > MAX_SINK_FPM:
            reasons.append(f"descending {sink:,.0f} ft/min")
        if reasons:
            fired.append(("high_energy_approach", "low",
                          "Below 1,000 ft: " + " and ".join(reasons) + "."))

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

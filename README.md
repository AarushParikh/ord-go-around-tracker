# ORD Go-Around Tracker

Detects go-arounds at Chicago O'Hare (KORD) from public ADS-B data, attaches the
weather in effect, infers a *likely* contributing factor with simple rules, and
shows it all in a Streamlit dashboard.

> **Status:** dashboard and draft rules are built. The data collector is being set
> up, so the dashboard currently runs on **demo data** (randomly generated, and
> clearly labelled as fake).

## Quick start (demo)

```bash
pip install -r requirements.txt
python make_demo_data.py      # builds data/demo.db with fake data
streamlit run app.py
```

When `data/tracker.db` (real data) exists, the dashboard uses it automatically.

## How it works

| Step | Script | What it does |
|---|---|---|
| 1 | `collector.py` | Polls the OpenSky Network API every 30 s for aircraft around ORD, stores positions in SQLite (`data/raw.db`). Runs as a service on a free Google Cloud VM (`deploy/install_vm.sh`); `status.py` is the health check. |
| 2 | `detect.py` | Rebuilds flights and detects go-arounds with the `traffic` library; counts arrivals for rates. *(to build)* |
| 3 | `weather.py` | Attaches the KORD METAR in effect at each go-around (Iowa Environmental Mesonet). *(to build)* |
| 4 | `classify.py` | Rule-based likely contributing factor + confidence + evidence. *(draft thresholds)* |
| 5 | `app.py` | Dashboard: filters, rates, breakdowns, map, event detail. |

Shared pieces: `config.py` (paths, airport facts, labels), `airlines.py`
(callsign → airline), `db.py` (database tables and loaders).

## Data sources
- **OpenSky Network** REST API: aircraft state vectors (position, altitude, speed).
- **Iowa Environmental Mesonet**: archived KORD METAR/SPECI reports.

## How go-arounds are detected
*(Fill in after Block 4.)* Summary: an approach aligned with a runway, followed
by a climb, followed by a second approach, using `traffic`'s `go_around()`.
Detection precision: **TBD**, measured by manually reviewing ~30 events.

## How factors are inferred
`classify.py` checks the evidence in priority order (windshear reported → runway
occupied → insufficient spacing → gusty/crosswind → low visibility/ceiling →
high-energy approach). The strongest rule that fires becomes the likely factor,
the next one the secondary factor. If none fire, the event is `insufficient_data`.
That's expected for many events.

These are **not official causes**. Only ATC and the crew know why a go-around was flown.
Thresholds are drafts until justified (see the top of `classify.py`).

## Known limitations
- Positions are sampled every ~30 s, so the go-around point is approximate (±30 s).
- ADS-B coverage near the ground is patchy, so "runway occupied" is capped at medium confidence.
- Missed go-arounds can't be measured (no public ground truth); only precision is measured.
- Groundspeed includes wind, so "high-energy approach" is a weak signal.
- METAR wind is true north and runway numbers are magnetic (a few degrees apart at ORD).
- Sample sizes per airline/runway are small; airlines under 500 arrivals are hidden from rate charts.

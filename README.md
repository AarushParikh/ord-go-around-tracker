# ORD Go-Around Tracker

Detects go-arounds at Chicago O'Hare (KORD) from public ADS-B data, attaches the
weather in effect, infers a *likely* contributing factor with simple rules, and
shows it all in a Streamlit dashboard.

> **Status:** dashboard, draft rules and data fetcher are built; the fetcher is being
> deployed. Until detection runs on real data, the dashboard shows **demo data**
> (randomly generated, and clearly labelled as fake).

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
| 1 | `fetch_adsblol.py` | Each day, streams adsb.lol's public flight-history archive and keeps every position inside a box around ORD below 15,000 ft (about 1 point per second near the airport) in SQLite (`data/raw.db`). Runs daily on a free Google Cloud VM (`deploy/install_vm.sh`); `status.py` is the health check. `collector.py` (live OpenSky polling) is kept for running at home, since OpenSky blocks cloud servers. |
| 2 | `detect.py` | Rebuilds flights and detects go-arounds with the `traffic` library; counts arrivals for rates. *(to build)* |
| 3 | `weather.py` | Attaches the KORD METAR/SPECI in effect at each go-around (latest report at or before it, max 90 min old) from Iowa Environmental Mesonet, and uses the altimeter setting to correct ADS-B pressure altitudes. |
| 4 | `classify.py` | Rule-based likely contributing factor + confidence + evidence. *(draft thresholds)* |
| 5 | `app.py` | Dashboard: filters, rates, breakdowns, map, event detail. |

Shared pieces: `config.py` (paths, airport facts, labels), `airlines.py`
(callsign → airline), `db.py` (database tables and loaders).

## Data sources
- **adsb.lol** daily history archive (main source): aircraft tracks from a community network of ADS-B receivers. Data © adsb.lol contributors, [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
- **OpenSky Network** REST API (optional, `collector.py`): live state vectors; blocked from cloud servers, usable from a home machine.
- **Iowa Environmental Mesonet**: archived KORD METAR/SPECI reports.

## How go-arounds are detected
`detect.py`, one day at a time:
1. Split each aircraft's positions into flights (a 20-minute gap starts a new flight).
2. Keep flights that start in the air (arrivals); skip departures and taxiing.
3. Find where each flight was lined up with an ORD runway (`traffic`'s `aligned_on_ils`).
   An approach that ends on the ground is an **arrival** (the denominator for rates).
4. Flights with two or more approaches are checked with `traffic`'s built-in
   `go_around("KORD")`: two landing attempts with a climb in between.
5. The go-around point is the lowest point of the aborted approach. The "aircraft ahead"
   is the last arrival on the same runway; spacing is measured when it touched down.

Test day (Nov 16, 2026): 1,327 arrivals, 4 go-arounds.
Detection precision: **TBD**, measured by manually reviewing ~30 events
(`python detect.py --plots 30`, fill in `data/verify/verify.csv`, then
`python detect.py --apply-verification data/verify/verify.csv`).

## How factors are inferred
`classify.py` checks the evidence in priority order (windshear reported → runway
occupied → insufficient spacing → gusty/crosswind → low visibility/ceiling →
high-energy approach). The strongest rule that fires becomes the likely factor,
the next one the secondary factor. If none fire, the event is `insufficient_data`.
That's expected for many events.

These are **not official causes**. Only ATC and the crew know why a go-around was flown.
Thresholds are drafts until justified (see the top of `classify.py`).

## Known limitations
- Coverage depends on adsb.lol's volunteer receivers; near ORD it is dense (~1 s between points), but gaps are possible.
- Positions are stored at 1-second resolution; "runway occupied" stays capped at medium confidence until checked against real events.
- Missed go-arounds can't be measured (no public ground truth); only precision is measured.
- Groundspeed includes wind, so "high-energy approach" is a weak signal.
- ADS-B barometric altitude is pressure altitude (29.92 inHg). It is corrected with the METAR altimeter setting (about 1,000 ft per inHg), which is a standard approximation, not an exact conversion.
- "Runway occupied" is inferred: the aircraft ahead touched down 0-90 s before the go-around and hadn't slowed below 30 kt. There is no runway-exit data.
- METAR wind is true north and runway numbers are magnetic (a few degrees apart at ORD).
- Sample sizes per airline/runway are small; airlines under 500 arrivals are hidden from rate charts.

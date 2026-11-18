"""
app.py — Streamlit dashboard for the ORD Go-Around Tracker.

Run locally:   streamlit run app.py

It reads data/tracker.db (real data). If that file doesn't exist yet it
falls back to data/demo.db (fake data from make_demo_data.py) and shows a
warning banner on every page.

Layout
------
Sidebar   filters: date range, airline, runway, factor, confidence
Top       KPIs: total go-arounds, rate per 1,000 arrivals, collecting since
Tabs      Trends | Breakdowns | Map | Event detail | Data
"""

from datetime import timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

import airlines
import config
import db

st.set_page_config(page_title="ORD Go-Around Tracker", page_icon="✈️", layout="wide")

# Map colors for each factor (RGB), reused by the charts so colors match.
FACTOR_COLORS = {
    "windshear_reported": (196, 69, 54),
    "runway_occupied": (230, 145, 56),
    "insufficient_spacing": (241, 196, 15),
    "gusty_wind_or_crosswind": (52, 152, 219),
    "low_visibility_or_ceiling": (142, 68, 173),
    "high_energy_approach": (39, 174, 96),
    "insufficient_data": (149, 165, 166),
}
FACTOR_SCALE = alt.Scale(
    domain=list(config.FACTOR_LABELS.values()),
    range=[f"rgb{FACTOR_COLORS[k]}" for k in config.FACTOR_LABELS],
)

# Airlines with fewer arrivals than this are hidden from the airline rate
# chart: one go-around in 200 landings would show a misleading "5 per 1,000".
MIN_ARRIVALS_FOR_RATE = 500


# ---------------------------------------------------------------------------
# Load data (cached so filters don't re-read the database every click)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)
def load_all(db_path: str):
    path = config.Path(db_path)
    meta = db.load_meta(path)
    events = db.load_events(path)
    arrivals = db.load_arrivals(path)
    tracks = db.load_tracks(path)

    # Add readable columns used throughout the app.
    tz = config.LOCAL_TZ
    events["time_local"] = events["go_around_time_utc"].dt.tz_convert(tz)
    events["date_local"] = events["time_local"].dt.date
    events["airline"] = events["airline_icao"].map(airlines.airline_name)
    events["airline_category"] = events["airline_icao"].map(airlines.airline_category)
    events["flow"] = events["runway"].map(config.airport_flow)
    events["factor"] = events["likely_factor"].map(config.FACTOR_LABELS).fillna("Not classified")
    events["confidence"] = events["confidence"].fillna("n/a")

    arrivals["time_local"] = arrivals["hour_utc"].dt.tz_convert(tz)
    arrivals["date_local"] = arrivals["time_local"].dt.date
    arrivals["airline_icao"] = arrivals["airline_icao"].replace("", None)
    arrivals["airline"] = arrivals["airline_icao"].map(airlines.airline_name)
    arrivals["flow"] = arrivals["runway"].map(config.airport_flow)
    return meta, events, arrivals, tracks


def pick_database():
    """Use real data if it exists, otherwise the demo database."""
    if config.TRACKER_DB.exists():
        return config.TRACKER_DB
    if config.DEMO_DB.exists():
        return config.DEMO_DB
    return None


db_path = pick_database()
if db_path is None:
    st.title("ORD Go-Around Tracker")
    st.info("No data yet. Run `python make_demo_data.py` to preview the dashboard with fake data, "
            "or run the pipeline to create `data/tracker.db`.")
    st.stop()

meta, events, arrivals, tracks = load_all(str(db_path))
is_demo = meta.get("is_demo") == "1"

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("✈️ ORD Go-Around Tracker")
st.caption("Go-arounds at Chicago O'Hare (KORD) detected from public ADS-B data (OpenSky Network), "
           "with the weather in effect and a rule-inferred likely contributing factor.")
if is_demo:
    st.error("**DEMO DATA.** Everything on this page is randomly generated to preview the dashboard. "
             "None of it is real.", icon="⚠️")

# ---------------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------------
st.sidebar.header("Filters")

min_date, max_date = arrivals["date_local"].min(), arrivals["date_local"].max()
date_range = st.sidebar.date_input("Date range (Chicago time)", value=(min_date, max_date),
                                   min_value=min_date, max_value=max_date)
# While the user is mid-selection Streamlit returns only one date.
start_date, end_date = (date_range if len(date_range) == 2 else (date_range[0], date_range[0]))

airline_options = sorted(events["airline"].dropna().unique())
sel_airlines = st.sidebar.multiselect("Airline", airline_options, placeholder="All airlines")

runway_options = sorted(events["runway"].dropna().unique())
sel_runways = st.sidebar.multiselect("Runway", runway_options, placeholder="All runways")

factor_options = [f for f in config.FACTOR_LABELS.values() if f in set(events["factor"])]
sel_factors = st.sidebar.multiselect(config.FACTOR_COLUMN_TITLE, factor_options,
                                     placeholder="All factors")

conf_options = [c for c in config.CONFIDENCE_LEVELS if c in set(events["confidence"])]
sel_conf = st.sidebar.multiselect("Confidence", conf_options, placeholder="All levels")

st.sidebar.divider()
st.sidebar.caption(f"Data file: `{db_path.name}`")
if "last_updated_utc" in meta:
    updated = pd.Timestamp(meta["last_updated_utc"]).tz_convert(config.LOCAL_TZ)
    st.sidebar.caption(f"Last updated: {updated:%b %d, %Y %H:%M} CT")


def apply_filters(df: pd.DataFrame, use_factor_filters: bool) -> pd.DataFrame:
    """Filter a table. Arrivals have no factor/confidence, so those filters are optional."""
    mask = (df["date_local"] >= start_date) & (df["date_local"] <= end_date)
    if sel_airlines:
        mask &= df["airline"].isin(sel_airlines)
    if sel_runways:
        mask &= df["runway"].isin(sel_runways)
    if use_factor_filters:
        if sel_factors:
            mask &= df["factor"].isin(sel_factors)
        if sel_conf:
            mask &= df["confidence"].isin(sel_conf)
    return df[mask]


ev = apply_filters(events, use_factor_filters=True)
arr = apply_filters(arrivals, use_factor_filters=False)


def rate_per_1000(n_goarounds: int, n_arrivals: int) -> float | None:
    return 1000 * n_goarounds / n_arrivals if n_arrivals else None


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
total_arrivals = int(arr["arrivals"].sum())
rate = rate_per_1000(len(ev), total_arrivals)
collecting_since = pd.Timestamp(meta.get("collection_start_utc", arrivals["hour_utc"].min()))
n_checked = int(ev["verified"].notna().sum())
n_confirmed = int((ev["verified"] == 1).sum())

k1, k2, k3, k4 = st.columns(4)
k1.metric("Go-arounds", f"{len(ev):,}")
k2.metric("Rate per 1,000 arrivals", f"{rate:.2f}" if rate is not None else "—",
          help="Go-arounds ÷ arrivals in the same filters × 1,000. Factor and confidence filters "
               "narrow the go-arounds only, giving e.g. the windshear-related rate.")
k3.metric("Arrivals in range", f"{total_arrivals:,}")
k4.metric("Collecting since", f"{collecting_since.tz_convert(config.LOCAL_TZ):%b %d, %Y}")
if n_checked:
    st.caption(f"Detection check: {n_confirmed} of {n_checked} manually reviewed events in this "
               f"selection were real go-arounds ({100 * n_confirmed / n_checked:.0f}% precision).")

if ev.empty:
    st.warning("No go-arounds match these filters.")
    st.stop()

tab_trend, tab_break, tab_map, tab_detail, tab_data = st.tabs(
    ["📈 Trends", "📊 Breakdowns", "🗺️ Map", "🔍 Event detail", "📄 Data"])

# ---------------------------------------------------------------------------
# Trends: rate over time
# ---------------------------------------------------------------------------
with tab_trend:
    period = st.radio("Group by", ["Day", "Week"], horizontal=True)
    freq = "D" if period == "Day" else "W-MON"

    def to_period(dates: pd.Series) -> pd.Series:
        return pd.to_datetime(dates).dt.to_period(freq).dt.start_time

    ga_by_period = ev.groupby(to_period(ev["date_local"])).size().rename("go_arounds")
    arr_by_period = arr.groupby(to_period(arr["date_local"]))["arrivals"].sum()
    trend = pd.concat([ga_by_period, arr_by_period], axis=1).fillna(0).reset_index(names="period")
    trend["rate"] = 1000 * trend["go_arounds"] / trend["arrivals"].where(trend["arrivals"] > 0)

    rate_chart = alt.Chart(trend).mark_line(point=True).encode(
        x=alt.X("period:T", title=None),
        y=alt.Y("rate:Q", title="Go-arounds per 1,000 arrivals"),
        tooltip=[alt.Tooltip("period:T", title=period), alt.Tooltip("go_arounds:Q", title="Go-arounds"),
                 alt.Tooltip("arrivals:Q", title="Arrivals", format=","),
                 alt.Tooltip("rate:Q", title="Rate", format=".2f")],
    ).properties(height=320)
    st.altair_chart(rate_chart, width="stretch")

    # Stacked count by factor underneath, to show WHAT drove the spikes.
    by_factor_time = ev.assign(period=to_period(ev["date_local"])).groupby(
        ["period", "factor"]).size().reset_index(name="count")
    stack = alt.Chart(by_factor_time).mark_bar().encode(
        x=alt.X("period:T", title=None),
        y=alt.Y("count:Q", title="Go-arounds"),
        color=alt.Color("factor:N", title=config.FACTOR_COLUMN_TITLE, scale=FACTOR_SCALE),
        tooltip=["period:T", "factor:N", "count:Q"],
    ).properties(height=260)
    st.altair_chart(stack, width="stretch")

    # Hour of day: when do go-arounds happen relative to traffic?
    st.subheader("By hour of day (Chicago time)")
    hourly = pd.concat([
        ev.groupby(ev["time_local"].dt.hour).size().rename("go_arounds"),
        arr.groupby(arr["time_local"].dt.hour)["arrivals"].sum(),
    ], axis=1).fillna(0).reset_index(names="hour")
    hourly["rate"] = 1000 * hourly["go_arounds"] / hourly["arrivals"].where(hourly["arrivals"] > 0)
    st.altair_chart(alt.Chart(hourly).mark_bar().encode(
        x=alt.X("hour:O", title="Hour"),
        y=alt.Y("rate:Q", title="Go-arounds per 1,000 arrivals"),
        tooltip=["hour", "go_arounds", "arrivals", alt.Tooltip("rate:Q", format=".2f")],
    ).properties(height=240), width="stretch")

# ---------------------------------------------------------------------------
# Breakdowns: runway, airline, factor, flow, airline category
# ---------------------------------------------------------------------------


def rate_table(group_col: str) -> pd.DataFrame:
    """Go-arounds, arrivals and rate for each value of a column."""
    table = pd.concat([
        ev.groupby(group_col).size().rename("go_arounds"),
        arr.groupby(group_col)["arrivals"].sum(),
    ], axis=1).fillna(0)
    table["rate"] = 1000 * table["go_arounds"] / table["arrivals"].where(table["arrivals"] > 0)
    return table.reset_index(names=group_col)


def rate_bar(table: pd.DataFrame, col: str, title: str):
    return alt.Chart(table).mark_bar().encode(
        y=alt.Y(f"{col}:N", sort="-x", title=None),
        x=alt.X("rate:Q", title="Go-arounds per 1,000 arrivals"),
        tooltip=[col, "go_arounds", alt.Tooltip("arrivals:Q", format=","),
                 alt.Tooltip("rate:Q", format=".2f")],
    ).properties(title=title, height=max(160, 28 * len(table)))


with tab_break:
    left, right = st.columns(2)

    with left:
        st.altair_chart(rate_bar(rate_table("runway"), "runway", "Rate by runway"),
                        width="stretch")
        st.altair_chart(rate_bar(rate_table("flow"), "flow", "Rate by airport flow"),
                        width="stretch")

    with right:
        by_airline = rate_table("airline")
        small = by_airline[by_airline["arrivals"] < MIN_ARRIVALS_FOR_RATE]
        by_airline = by_airline[by_airline["arrivals"] >= MIN_ARRIVALS_FOR_RATE]
        st.altair_chart(rate_bar(by_airline, "airline", "Rate by airline"),
                        width="stretch")
        if not small.empty:
            st.caption(f"Hidden (fewer than {MIN_ARRIVALS_FOR_RATE:,} arrivals, rate too noisy): "
                       + ", ".join(small["airline"]))

        factor_counts = ev.groupby("factor").size().reset_index(name="count")
        st.altair_chart(alt.Chart(factor_counts).mark_bar().encode(
            y=alt.Y("factor:N", sort="-x", title=None),
            x=alt.X("count:Q", title="Go-arounds"),
            color=alt.Color("factor:N", scale=FACTOR_SCALE, legend=None),
            tooltip=["factor", "count"],
        ).properties(title=config.FACTOR_COLUMN_TITLE, height=240), width="stretch")

    st.subheader("Confidence of the inferred factor")
    conf_table = pd.crosstab(ev["factor"], ev["confidence"]).reindex(
        columns=[c for c in config.CONFIDENCE_LEVELS if c in set(ev["confidence"])])
    st.dataframe(conf_table, width="stretch")
    st.caption("Factors are inferred by simple rules from trajectory and METAR data. They are not "
               "official causes: only ATC and the flight crew know why a go-around was flown.")

# ---------------------------------------------------------------------------
# Map of all filtered go-around tracks
# ---------------------------------------------------------------------------


def track_paths(event_ids) -> pd.DataFrame:
    """One row per event with its path as a list of [lon, lat] points, for pydeck."""
    sel = tracks[tracks["event_id"].isin(event_ids)]
    paths = sel.groupby("event_id")[["lon", "lat"]].apply(lambda d: d.values.tolist())
    df = paths.rename("path").reset_index().merge(
        ev[["event_id", "callsign", "runway", "factor", "likely_factor", "time_local"]], on="event_id")
    df["color"] = df["likely_factor"].map(lambda f: list(FACTOR_COLORS.get(f, (100, 100, 100))))
    df["when"] = df["time_local"].dt.strftime("%b %d %H:%M")
    return df.drop(columns=["time_local"])


def ord_view(zoom=9.5):
    return pdk.ViewState(latitude=config.AIRPORT_LAT, longitude=config.AIRPORT_LON, zoom=zoom)


with tab_map:
    max_tracks = 300
    shown = ev.sort_values("go_around_time_utc", ascending=False).head(max_tracks)
    if len(ev) > max_tracks:
        st.caption(f"Showing the {max_tracks} most recent of {len(ev)} go-arounds.")
    paths = track_paths(shown["event_id"])
    st.pydeck_chart(pdk.Deck(
        layers=[pdk.Layer("PathLayer", paths, get_path="path", get_color="color",
                          width_min_pixels=2, opacity=0.6, pickable=True)],
        initial_view_state=ord_view(),
        tooltip={"text": "{callsign} · RWY {runway}\n{when} CT\n{factor}"},
        map_provider="carto", map_style="light",
    ))
    st.markdown(" · ".join(
        f"<span style='color:rgb{FACTOR_COLORS[k]}'>■</span> {label}"
        for k, label in config.FACTOR_LABELS.items()), unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Event detail
# ---------------------------------------------------------------------------


def fmt(value, pattern="{}", missing="—"):
    return missing if value is None or pd.isna(value) else pattern.format(value)


with tab_detail:
    ordered = ev.sort_values("go_around_time_utc", ascending=False)
    labels = {row.event_id: f"{row.time_local:%b %d %H:%M} CT · {row.callsign} · RWY {row.runway} · {row.factor}"
              for row in ordered.itertuples()}
    chosen = st.selectbox("Choose a go-around", list(labels), format_func=labels.get)
    e = ordered.set_index("event_id").loc[chosen]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Callsign", e["callsign"])
    c2.metric("Runway", e["runway"])
    c3.metric("Altitude at go-around",
              fmt(e["ga_alt_ft"] - config.AIRPORT_ELEV_FT, "{:,.0f} ft AGL"))
    c4.metric("Groundspeed", fmt(e["ga_groundspeed_kt"], "{:.0f} kt"))

    st.markdown(f"**{e['airline']}** ({e['airline_category']}) · go-around at "
                f"**{e['time_local']:%a %b %d, %Y %H:%M:%S} CT** · "
                f"landed **{fmt(e['minutes_to_landing'], '{:.1f} min')}** later"
                + (f" on {e['landing_runway']}" if pd.notna(e["landing_runway"]) else ""))

    # The model output, always labelled as inferred.
    st.subheader(config.FACTOR_COLUMN_TITLE)
    secondary = config.FACTOR_LABELS.get(e["secondary_factor"]) if pd.notna(e["secondary_factor"]) else None
    st.info(f"**{e['factor']}** (confidence: {e['confidence']})"
            + (f" · secondary: {secondary}" if secondary else "")
            + f"\n\nEvidence: {fmt(e['evidence'])}")

    left, right = st.columns([3, 2])
    with left:
        one = track_paths([chosen])
        pts = tracks[tracks["event_id"] == chosen]
        st.pydeck_chart(pdk.Deck(
            layers=[
                pdk.Layer("PathLayer", one, get_path="path", get_color="color", width_min_pixels=3),
                pdk.Layer("ScatterplotLayer", pts, get_position=["lon", "lat"], get_radius=120,
                          get_fill_color=[30, 30, 30, 160]),
            ],
            initial_view_state=ord_view(zoom=10), map_provider="carto", map_style="light"))
        profile = pts.assign(
            minutes=(pts["time_utc"] - e["go_around_time_utc"]).dt.total_seconds() / 60)
        st.altair_chart(alt.Chart(profile).mark_line(point=True).encode(
            x=alt.X("minutes:Q", title="Minutes from go-around"),
            y=alt.Y("alt_ft:Q", title="Altitude (ft MSL)"),
            tooltip=[alt.Tooltip("minutes:Q", format=".1f"), "alt_ft"],
        ).properties(title="Altitude profile", height=220), width="stretch")

    with right:
        st.markdown("**Weather (METAR in effect)**")
        st.code(fmt(e["metar_raw"], missing="No METAR attached"), language=None)
        wind = ("calm/variable" if pd.isna(e["wind_dir_deg"])
                else f"{e['wind_dir_deg']:03.0f}° at {e['wind_kt']:.0f} kt")
        if pd.notna(e["gust_kt"]):
            wind += f", gusts {e['gust_kt']:.0f} kt"
        st.markdown(
            f"- Wind: {wind}\n"
            f"- Visibility: {fmt(e['visibility_sm'], '{:g} SM')}\n"
            f"- Ceiling: {fmt(e['ceiling_ft'], '{:,.0f} ft', missing='none reported')}\n"
            f"- Weather: {e['wx_codes'] or 'none'}\n"
            f"- Windshear reported: {'yes' if e['llws'] == 1 else 'no'}")

        st.markdown("**Spacing to the preceding arrival**")
        on_rwy = {1: "yes", 0: "no"}.get(e["prev_on_runway"], "unknown")
        st.markdown(
            f"- Preceding arrival: {fmt(e['prev_callsign'])}\n"
            f"- Distance: {fmt(e['prev_spacing_nm'], '{:.1f} nm')}\n"
            f"- Time: {fmt(e['prev_spacing_s'], '{:.0f} s')}\n"
            f"- Still on/near runway: {on_rwy}")

        verified = {1: "✅ confirmed real", 0: "❌ false detection"}.get(e["verified"], "not yet reviewed")
        st.caption(f"Manual check: {verified}")

# ---------------------------------------------------------------------------
# Raw table + download
# ---------------------------------------------------------------------------
with tab_data:
    columns = ["time_local", "callsign", "airline", "runway", "ga_alt_ft", "ga_groundspeed_kt",
               "minutes_to_landing", "prev_spacing_nm", "prev_on_runway", "factor", "confidence",
               "evidence", "metar_raw", "verified"]
    table = ev[columns].sort_values("time_local", ascending=False).rename(
        columns={"factor": config.FACTOR_COLUMN_TITLE, "time_local": "time (CT)"})
    st.dataframe(table, width="stretch", hide_index=True)
    st.download_button("Download CSV", table.to_csv(index=False).encode(),
                       file_name="ord_go_arounds.csv", mime="text/csv")

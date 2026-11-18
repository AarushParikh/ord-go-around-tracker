"""
test_opensky.py — One-shot check that OpenSky accepts our credentials from
this machine (run it on the cloud VM first: Block A2).

Uses only Python's standard library, so it runs on a fresh VM with no installs.

Usage:  python3 test_opensky.py            (expects credentials.json next to it)
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"
ORD_BOX = {"lamin": 41.80, "lomin": -88.10, "lamax": 42.10, "lomax": -87.70}

creds = json.loads((Path(__file__).parent / "credentials.json").read_text())

# Step 1: trade the client id/secret for a short-lived access token.
body = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": creds["clientId"],
    "client_secret": creds["clientSecret"],
}).encode()
try:
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as r:
        token = json.load(r)["access_token"]
    print("1/2  Token OK")
except Exception as e:
    sys.exit(f"1/2  Token FAILED: {e}\n     Check credentials.json, or OpenSky may be blocking this network.")

# Step 2: ask for every aircraft in the box around O'Hare.
url = STATES_URL + "?" + urllib.parse.urlencode(ORD_BOX)
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
        remaining = r.headers.get("X-Rate-Limit-Remaining", "not reported")
except Exception as e:
    sys.exit(f"2/2  Data request FAILED: {e}\n     If this times out or says 403, OpenSky is likely blocking cloud IPs.")

states = data.get("states") or []
print(f"2/2  Data OK: {len(states)} aircraft near ORD right now. Credits remaining today: {remaining}")
for s in states[:10]:
    callsign = (s[1] or "").strip() or "(none)"
    alt = f"{s[7] * 3.281:,.0f} ft" if s[7] is not None else "on ground/unknown"
    print(f"     {callsign:<9} {alt}")

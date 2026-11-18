"""
airlines.py — Turn a callsign like 'UAL1234' into an airline name.

The first three letters of an airline callsign are the ICAO airline code.
Regional carriers (SkyWest, Envoy, Republic...) fly under their own codes
even when the plane is painted "United Express" or "American Eagle", so we
also record which mainline brand they fly for at ORD.
"""

# ICAO code -> (airline name, category)
AIRLINES = {
    "UAL": ("United Airlines", "Mainline"),
    "AAL": ("American Airlines", "Mainline"),
    "DAL": ("Delta Air Lines", "Mainline"),
    "SWA": ("Southwest Airlines", "Mainline"),
    "ASA": ("Alaska Airlines", "Mainline"),
    "JBU": ("JetBlue", "Mainline"),
    "NKS": ("Spirit Airlines", "Mainline"),
    "FFT": ("Frontier Airlines", "Mainline"),
    "SKW": ("SkyWest (United Express / American Eagle)", "Regional"),
    "ENY": ("Envoy (American Eagle)", "Regional"),
    "RPA": ("Republic (United Express / American Eagle)", "Regional"),
    "ASH": ("Mesa (United Express)", "Regional"),
    "GJS": ("GoJet (United Express)", "Regional"),
    "JIA": ("PSA (American Eagle)", "Regional"),
    "EDV": ("Endeavor (Delta Connection)", "Regional"),
    "ACA": ("Air Canada", "International"),
    "DLH": ("Lufthansa", "International"),
    "BAW": ("British Airways", "International"),
    "FDX": ("FedEx", "Cargo"),
    "UPS": ("UPS Airlines", "Cargo"),
}


def airline_code(callsign: str | None) -> str | None:
    """Return the 3-letter ICAO code if the callsign looks like an airline one."""
    if not callsign:
        return None
    callsign = callsign.strip().upper()
    prefix = callsign[:3]
    # Airline callsigns are 3 letters followed by a flight number.
    # Private aircraft use registrations like 'N123AB', which we skip.
    if len(callsign) > 3 and prefix.isalpha() and callsign[3].isdigit():
        return prefix
    return None


def airline_name(code: str | None) -> str:
    """Readable airline name, or the raw code if we don't know it."""
    if code is None:
        return "Unknown / non-airline"
    return AIRLINES.get(code, (code, "Other"))[0]


def airline_category(code: str | None) -> str:
    if code is None:
        return "Other"
    return AIRLINES.get(code, (code, "Other"))[1]

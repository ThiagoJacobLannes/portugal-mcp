"""The one FastMCP instance every feature registers its tools on."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

BASE_INSTRUCTIONS = (
    "Portugal's public data in one server: weather and warnings (IPMA), places and postal codes, "
    "official statistics (INE, Banco de Portugal), Parliament, public contracts, the national "
    "open-data portal, the health service (SNS), medicines and pharmacies, fuel and EV charging, "
    "Lisbon-area buses, train, metro, bus and tram timetables (CP, Metro de Lisboa, Carris, Fertagus) "
    "wildfire burnt areas (EFFIS) and live civil-protection incidents (ANEPC). Everything is read-only public data. Answer in the user's language and "
    "always say which source and which date or period each figure comes from."
)

mcp = FastMCP("portugal", instructions=BASE_INSTRUCTIONS)

_notes: dict[str, str] = {}


def note(feature: str, text: str) -> None:
    """Add a feature's usage rules to the server instructions (only for loaded features)."""
    _notes[feature] = text


def instructions() -> str:
    return BASE_INSTRUCTIONS + "".join(f"\n\n[{k}] {v}" for k, v in _notes.items())

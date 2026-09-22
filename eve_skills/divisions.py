"""Corporation hangar division names shared by industry and inventory views."""

from __future__ import annotations

from . import esi as esi_mod

SCOPE = "esi-corporations.read_divisions.v1"


def number(location_flag: str | None) -> int | None:
    """Return 1..7 for CorpSAG1..CorpSAG7; every other asset flag is not a division."""
    text = location_flag or ""
    if not text.startswith("CorpSAG") or not text[7:].isdigit():
        return None
    value = int(text[7:])
    return value if 1 <= value <= 7 else None


def label(location_flag: str | None, names: dict[int, str] | None = None) -> str:
    """Player-facing division name, numbered fallback, or an unchanged non-hangar flag."""
    division = number(location_flag)
    if division is None:
        return location_flag or "-"
    return (names or {}).get(division) or f"division {division}"


def fetch(client, token_record: dict, corporation_id: int) -> tuple[dict[int, str], str | None]:
    """Read a corporation's named hangars, or return numbered fallbacks plus one actionable note."""
    name = token_record.get("character_name") or str(token_record.get("character_id", "?"))
    if SCOPE not in set(token_record.get("scopes") or []):
        return {}, (f"{name}: corporation hangar names unavailable; showing numbered divisions - "
                    "run: eve-skills login --scopes divisions")
    try:
        document = client.get(
            f"/corporations/{corporation_id}/divisions", token=token_record["access_token"]
        )
    except esi_mod.AuthError as err:
        return {}, (f"{name}: corporation hangar names unavailable ({err}); showing numbered "
                    "divisions - the endpoint requires the Director role")
    hangars = document.get("hangar", []) if isinstance(document, dict) else []
    names = {}
    for row in hangars:
        if not isinstance(row, dict):
            continue
        try:
            division = int(row.get("division"))
        except (TypeError, ValueError):
            continue
        title = row.get("name")
        if 1 <= division <= 7 and isinstance(title, str) and title.strip():
            names[division] = title.strip()
    return names, None


def resolve(spec: str | None, names: dict[int, str]) -> int | None:
    """Resolve --division as a number or unique case-insensitive player name."""
    if spec is None:
        return None
    text = str(spec).strip()
    if text.isdigit():
        value = int(text)
        if 1 <= value <= 7:
            return value
        raise RuntimeError("--division must be a corporation hangar number from 1 to 7")
    matches = [division for division, name in names.items() if name.casefold() == text.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not names:
        raise RuntimeError(
            f"cannot resolve division name '{text}' without divisions consent; use its number 1..7"
        )
    available = ", ".join(f"{division}={name}" for division, name in sorted(names.items()))
    raise RuntimeError(f"no corporation hangar division named '{text}'; choices: {available}")

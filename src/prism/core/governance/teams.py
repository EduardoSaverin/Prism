"""What makes a valid team name."""

from __future__ import annotations

import re

MAX_TEAM_NAME_LENGTH = 128

_TEAM_NAME = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_TEAM_NAME_LENGTH - 1}}}")


def check_team_name(name: str) -> str:
    """Return the name, or raise ValueError if it is not a valid team name.

    A name is 1 to 128 characters: letters, digits, '.', '_' and '-', starting
    with a letter or digit. This keeps names easy to type in the CLI and in URLs.
    """
    if not name:
        raise ValueError("A team name cannot be empty.")
    if len(name) > MAX_TEAM_NAME_LENGTH:
        raise ValueError(f"A team name can be at most {MAX_TEAM_NAME_LENGTH} characters.")
    if not _TEAM_NAME.fullmatch(name):
        raise ValueError(
            f"'{name}' is not a valid team name. Use letters, digits, '.', '_' and '-', "
            "starting with a letter or digit."
        )
    return name

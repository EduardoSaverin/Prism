import pytest

from prism.core.governance.teams import MAX_TEAM_NAME_LENGTH, check_team_name


@pytest.mark.parametrize("name", ["ml", "Data-Platform", "team_1", "a.b", "7", "x" * 128])
def test_valid_team_names_are_accepted(name: str) -> None:
    assert check_team_name(name) == name


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "cannot be empty"),
        ("x" * (MAX_TEAM_NAME_LENGTH + 1), "at most 128"),
        (" ", "not a valid team name"),
        (" ml", "not a valid team name"),
        ("ml ", "not a valid team name"),
        ("my team", "not a valid team name"),
        ("-ml", "not a valid team name"),
        (".ml", "not a valid team name"),
        ("ml/eng", "not a valid team name"),
        ("ml\n", "not a valid team name"),
    ],
)
def test_invalid_team_names_are_refused(name: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_team_name(name)

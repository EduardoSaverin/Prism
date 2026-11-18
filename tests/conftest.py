from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def example_config_path() -> Path:
    return ROOT / "config" / "prism.example.yaml"

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finder.config import load_config  # noqa: E402
from finder.store import Store  # noqa: E402


@pytest.fixture
def config():
    return load_config(Path(__file__).resolve().parents[1] / "finder.example.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "finder.db"))

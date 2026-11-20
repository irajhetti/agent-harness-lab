from pathlib import Path

import pytest

from heal_loop import HealConfig
from heal_loop.fixture import make_fixture


@pytest.fixture
def cfg(tmp_path: Path) -> HealConfig:
    """A fresh toy repository with a failing job, in its own temp dir."""
    return make_fixture(tmp_path)

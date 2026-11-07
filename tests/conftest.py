from collections.abc import Iterator
from pathlib import Path

import pytest

from rig import Rig


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    instance = Rig(tmp_path, monkeypatch)
    yield instance
    instance.finish()

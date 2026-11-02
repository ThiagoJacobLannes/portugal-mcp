import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Never touch the user's real cache from tests."""
    monkeypatch.setenv("PORTUGAL_MCP_CACHE_DIR", str(tmp_path / "cache"))

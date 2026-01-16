import pytest


def test_imports():
    """Simple smoke test to ensure the module can be imported."""
    try:
        import repl_mcp  # noqa: F401
    except ImportError as e:
        pytest.fail(f"Failed to import repl_mcp: {e}")


def test_version_in_toml_matches_code(monkeypatch):
    """
    If we had a version in the code, we'd check it here.
    For now, just ensuring we can read pyproject.toml
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # Python 3.10 fallback

    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "repl-mcp"

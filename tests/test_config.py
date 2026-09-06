"""``.env`` resolution.

The path used to be hardcoded to ``<package>/../../../.env`` — correct for a
source checkout and meaningless from ``site-packages``, so every env var was
silently ignored for an installed wheel. That is a supported mode:
``pyproject.toml`` ships a console script and ``.env.example`` tells operators
to set ``CACHE_DIR`` "when running from an installed wheel".
"""

import importlib
from pathlib import Path

import pytest

from academic_tools_mcp import config


def _reload_with(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    return importlib.reload(config)


class TestCandidateOrder:
    def test_explicit_override_is_first(self, monkeypatch, tmp_path):
        target = tmp_path / "custom.env"
        target.write_text("X=1\n")
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", str(target))
        assert config._candidate_env_paths()[0] == target

    def test_source_checkout_precedes_cwd(self, monkeypatch):
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        paths = config._candidate_env_paths()
        # Existing source-tree setups must keep resolving exactly as before.
        assert paths[0] == Path(config.__file__).resolve().parent.parent.parent / ".env"

    def test_xdg_config_home_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert tmp_path / "academic-tools-mcp" / ".env" in config._candidate_env_paths()

    def test_falls_back_to_dot_config_without_xdg(self, monkeypatch):
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        expected = Path.home() / ".config" / "academic-tools-mcp" / ".env"
        assert expected in config._candidate_env_paths()

    def test_tilde_is_expanded_in_override(self, monkeypatch):
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", "~/somewhere/.env")
        assert "~" not in str(config._candidate_env_paths()[0])


class TestLoading:
    def test_explicit_file_is_loaded(self, monkeypatch, tmp_path):
        env = tmp_path / "custom.env"
        env.write_text("ATM_TEST_ONLY=from-file\n")
        monkeypatch.delenv("ATM_TEST_ONLY", raising=False)

        reloaded = _reload_with(monkeypatch, ACADEMIC_TOOLS_ENV_FILE=str(env))
        try:
            assert env == reloaded.ENV_FILE
            assert reloaded.get("ATM_TEST_ONLY") == "from-file"
        finally:
            importlib.reload(config)

    def test_real_environment_wins_over_file(self, monkeypatch, tmp_path):
        env = tmp_path / "custom.env"
        env.write_text("ATM_TEST_ONLY=from-file\n")

        reloaded = _reload_with(
            monkeypatch,
            ACADEMIC_TOOLS_ENV_FILE=str(env),
            ATM_TEST_ONLY="from-environment",
        )
        try:
            # load_dotenv is called without override, so an exported value
            # takes effect no matter what any file says.
            assert reloaded.get("ATM_TEST_ONLY") == "from-environment"
        finally:
            importlib.reload(config)

    def test_missing_file_is_not_an_error(self, monkeypatch, tmp_path):
        reloaded = _reload_with(monkeypatch, ACADEMIC_TOOLS_ENV_FILE=str(tmp_path / "nope.env"))
        try:
            assert reloaded.ENV_FILE is None or reloaded.ENV_FILE.is_file()
        finally:
            importlib.reload(config)

    def test_unreadable_candidate_does_not_stop_the_search(self, monkeypatch, tmp_path):
        # A directory where a file is expected raises on read; the rest of the
        # candidate list must still be tried.
        bogus = tmp_path / "adir.env"
        bogus.mkdir()
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", str(bogus))
        assert config._load_env() != bogus


class TestGet:
    @pytest.mark.parametrize("raw", ["", None])
    def test_empty_reads_as_unset(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("ATM_TEST_ONLY", raising=False)
        else:
            monkeypatch.setenv("ATM_TEST_ONLY", raw)
        assert config.get("ATM_TEST_ONLY") is None

    def test_value_is_returned(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_ONLY", "v")
        assert config.get("ATM_TEST_ONLY") == "v"


class TestFlag:
    """The single home for env-var truthiness. Two call sites spelling their
    own ``in ("1", "true", ...)`` is how ``DEBUG_REQUESTS=yes`` ends up
    meaning something different from ``ENABLE_DEBUG_TOOLS=yes``.
    """

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "Yes", "on", "ON", " 1 ", "on\n"])
    def test_enabled_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("ATM_TEST_FLAG", raw)
        assert config.flag("ATM_TEST_FLAG") is True

    @pytest.mark.parametrize("raw", ["", "0", "no", "off", "false", "nope", " "])
    def test_disabled_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("ATM_TEST_FLAG", raw)
        assert config.flag("ATM_TEST_FLAG") is False

    def test_unset_is_false(self, monkeypatch):
        monkeypatch.delenv("ATM_TEST_FLAG", raising=False)
        assert config.flag("ATM_TEST_FLAG") is False

    def test_read_at_call_time(self, monkeypatch):
        """Both callers re-check per request so an operator can flip the flag
        without restarting the server."""
        monkeypatch.delenv("ATM_TEST_FLAG", raising=False)
        assert config.flag("ATM_TEST_FLAG") is False
        monkeypatch.setenv("ATM_TEST_FLAG", "1")
        assert config.flag("ATM_TEST_FLAG") is True

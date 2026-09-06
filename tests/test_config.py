"""``.env`` resolution and the three config accessors.

The path used to be hardcoded to ``<package>/../../../.env`` — correct for a
source checkout and meaningless from ``site-packages``, so every env var was
silently ignored for an installed wheel. That is a supported mode:
``pyproject.toml`` ships a console script and ``.env.example`` tells operators
to set ``CACHE_DIR`` "when running from an installed wheel".
"""

import contextlib
import importlib
import math
import os
from pathlib import Path

import pytest

from academic_tools_mcp import config
from tests.conftest import _EMPTY_ENV_FILE

_PROJECT_ROOT_ENV = Path(config.__file__).resolve().parent.parent.parent / ".env"


@contextlib.contextmanager
def _reloaded(monkeypatch, **env):
    """Reload ``config`` under ``env`` so ``_load_env`` re-runs, then restore.

    Cleanup is by hand rather than by monkeypatch: ``load_dotenv`` writes
    straight to ``os.environ``, so monkeypatch records nothing for a key that
    did not exist when the test started and would leak it into every test
    after this one.
    """
    before = set(os.environ)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    try:
        yield importlib.reload(config)
    finally:
        for key in set(os.environ) - before:
            del os.environ[key]
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", _EMPTY_ENV_FILE)
        importlib.reload(config)


class TestCandidateOrder:
    def test_an_override_is_the_only_candidate(self, monkeypatch, tmp_path):
        """Authoritative: a named file never falls back to another one.

        Fall-through meant a typo'd path silently loaded whichever ``.env``
        happened to be next in the list — a different operator's config, with
        no complaint.
        """
        target = tmp_path / "custom.env"
        target.write_text("X=1\n")
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", str(target))
        assert config._candidate_env_paths() == [target]

    def test_a_missing_override_still_shadows_the_implicit_paths(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", str(tmp_path / "nope.env"))
        assert config._candidate_env_paths() == [tmp_path / "nope.env"]
        assert config._load_env() is None

    def test_tilde_is_expanded_in_override(self, monkeypatch):
        monkeypatch.setenv("ACADEMIC_TOOLS_ENV_FILE", "~/somewhere/.env")
        assert "~" not in str(config._candidate_env_paths()[0])

    def test_implicit_order_is_root_then_cwd_then_xdg(self, monkeypatch, tmp_path):
        """The whole documented order, not just its first entry.

        Existing source-tree setups must keep resolving exactly as before, so
        the project root stays ahead of ``$PWD``.
        """
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config._candidate_env_paths() == [
            _PROJECT_ROOT_ENV,
            Path.cwd() / ".env",
            tmp_path / "xdg" / "academic-tools-mcp" / ".env",
        ]

    def test_falls_back_to_dot_config_without_xdg(self, monkeypatch):
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        expected = Path.home() / ".config" / "academic-tools-mcp" / ".env"
        assert expected in config._candidate_env_paths()

    def test_a_deleted_working_directory_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        """``Path.cwd()`` raises when the cwd is gone.

        It runs at import, so an unguarded raise here takes the whole server
        down rather than costing one candidate.
        """
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        def _boom():
            raise FileNotFoundError("cwd was removed")

        monkeypatch.setattr(config.Path, "cwd", staticmethod(_boom))
        # Only the $PWD candidate is lost; the ones after it are still tried.
        assert config._candidate_env_paths() == [
            _PROJECT_ROOT_ENV,
            tmp_path / "academic-tools-mcp" / ".env",
        ]

    def test_an_unresolvable_home_is_skipped_not_fatal(self, monkeypatch):
        """``Path.home()`` raises RuntimeError when home can't be determined."""
        monkeypatch.delenv("ACADEMIC_TOOLS_ENV_FILE", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        def _boom():
            raise RuntimeError("could not determine home directory")

        monkeypatch.setattr(config.Path, "home", staticmethod(_boom))
        assert config._candidate_env_paths() == [_PROJECT_ROOT_ENV, Path.cwd() / ".env"]


class TestLoading:
    def test_explicit_file_is_loaded(self, monkeypatch, tmp_path):
        env = tmp_path / "custom.env"
        env.write_text("ATM_TEST_ONLY=from-file\n")

        with _reloaded(monkeypatch, ACADEMIC_TOOLS_ENV_FILE=str(env)) as reloaded:
            assert env == reloaded.ENV_FILE
            assert reloaded.get("ATM_TEST_ONLY") == "from-file"

        assert "ATM_TEST_ONLY" not in os.environ, "a loaded value leaked past the test"

    def test_real_environment_wins_over_file(self, monkeypatch, tmp_path):
        env = tmp_path / "custom.env"
        env.write_text("ATM_TEST_ONLY=from-file\n")

        with _reloaded(
            monkeypatch,
            ACADEMIC_TOOLS_ENV_FILE=str(env),
            ATM_TEST_ONLY="from-environment",
        ) as reloaded:
            # load_dotenv is called without override, so an exported value
            # takes effect no matter what any file says.
            assert reloaded.get("ATM_TEST_ONLY") == "from-environment"

    def test_env_file_records_a_non_explicit_winner(self, monkeypatch, tmp_path):
        winner = tmp_path / "won.env"
        winner.write_text("ATM_TEST_ONLY=yes\n")
        monkeypatch.setattr(
            config, "_candidate_env_paths", lambda: [tmp_path / "absent.env", winner]
        )
        try:
            assert config._load_env() == winner
        finally:
            os.environ.pop("ATM_TEST_ONLY", None)

    def test_no_candidate_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_candidate_env_paths", lambda: [tmp_path / "absent.env"])
        assert config._load_env() is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
    def test_an_unreadable_candidate_does_not_stop_the_search(self, monkeypatch, tmp_path):
        """A candidate we can't even stat must not shadow the rest.

        ``Path.is_file()`` swallows ENOENT, ENOTDIR and ELOOP itself, so the
        only way to reach the guard is a directory we lack permission to
        traverse — EACCES.
        """
        locked = tmp_path / "locked"
        locked.mkdir()
        winner = tmp_path / "won.env"
        winner.write_text("# empty\n")
        locked.chmod(0o000)
        monkeypatch.setattr(
            config, "_candidate_env_paths", lambda: [locked / "unreachable.env", winner]
        )
        try:
            assert config._load_env() == winner
        finally:
            locked.chmod(0o755)

    def test_a_non_utf8_candidate_does_not_stop_the_search(self, monkeypatch, tmp_path):
        """UnicodeDecodeError escapes ``load_dotenv``, not just OSError.

        A ``.env`` saved as UTF-16 is an ordinary operator mistake; uncaught it
        aborts the import and the console script won't start at all.
        """
        binary = tmp_path / "binary.env"
        binary.write_bytes(b"A=1\n\xff\xfe\x00\n")
        winner = tmp_path / "won.env"
        winner.write_text("# empty\n")
        monkeypatch.setattr(config, "_candidate_env_paths", lambda: [binary, winner])
        assert config._load_env() == winner


class TestGet:
    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n", None])
    def test_empty_or_blank_reads_as_unset(self, monkeypatch, raw):
        """A blank value is a typo, not a setting.

        ``CROSSREF_MAILTO="   "`` used to be truthy, so ``in_polite_pool()``
        said yes and the client took the polite tier's rates while
        ``normalize_mailto`` dropped the address and sent no contact at all.
        """
        if raw is None:
            monkeypatch.delenv("ATM_TEST_ONLY", raising=False)
        else:
            monkeypatch.setenv("ATM_TEST_ONLY", raw)
        assert config.get("ATM_TEST_ONLY") is None

    def test_value_is_returned(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_ONLY", "v")
        assert config.get("ATM_TEST_ONLY") == "v"

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_ONLY", "  v\n")
        assert config.get("ATM_TEST_ONLY") == "v"

    def test_interior_whitespace_survives(self, monkeypatch):
        # Converter command templates and paths are full of it.
        monkeypatch.setenv("ATM_TEST_ONLY", " my-tool --in {input} ")
        assert config.get("ATM_TEST_ONLY") == "my-tool --in {input}"


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


class TestNumber:
    """The single home for "a limit an operator can turn off".

    Two modules spelling their own ``{"none", "off", ...}`` is how
    ``MAX_PDF_BYTES`` came to accept ``disabled`` on one policy and
    ``PDF_CONVERT_TIMEOUT`` a bare ``-1`` on another.
    """

    def _num(self, key="ATM_TEST_NUM", default=100, *, cast=int, policy="default"):
        return config.number(key, default, cast=cast, on_nonpositive=policy)

    def test_unset_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("ATM_TEST_NUM", raising=False)
        assert self._num() == 100

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_uses_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("ATM_TEST_NUM", raw)
        assert self._num() == 100

    @pytest.mark.parametrize("raw", ["none", "off", "disabled", "0", "NONE", "Off", " none "])
    def test_the_disable_vocabulary(self, monkeypatch, raw):
        monkeypatch.setenv("ATM_TEST_NUM", raw)
        assert self._num() is None

    def test_a_positive_value_is_returned(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_NUM", " 4096 ")
        assert self._num() == 4096

    def test_garbage_uses_the_default(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_NUM", "not-a-number")
        assert self._num() == 100

    @pytest.mark.parametrize("raw", ["-1", "-4096"])
    def test_negative_under_the_default_policy(self, monkeypatch, raw):
        """``on_nonpositive="default"`` — a negative cap is a typo.

        "-1" reads as an "unlimited" idiom in other tools. Honouring it here
        would silently remove the guard the setting exists to provide.
        """
        monkeypatch.setenv("ATM_TEST_NUM", raw)
        assert self._num() == 100

    @pytest.mark.parametrize("raw", ["-1", "-0.5"])
    def test_negative_under_the_disable_policy(self, monkeypatch, raw):
        monkeypatch.setenv("ATM_TEST_NUM", raw)
        assert self._num(default=100.0, cast=float, policy="disable") is None

    def test_fractions_need_a_float_cast(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_NUM", "90.5")
        assert self._num(default=100.0, cast=float, policy="disable") == 90.5
        # int() rejects it outright, which is the size cap's whole point:
        # half a byte is not a cap, it is a typo.
        assert self._num() == 100

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity"])
    @pytest.mark.parametrize("policy", ["default", "disable"])
    def test_non_finite_uses_the_default(self, monkeypatch, raw, policy):
        """``nan`` compares false against every bound.

        ``float("nan")`` is neither ``> 0`` nor ``<= 0``, so it slipped past
        both branches and reached ``asyncio.wait_for`` as a timeout nothing
        could satisfy.
        """
        monkeypatch.setenv("ATM_TEST_NUM", raw)
        got = self._num(default=100.0, cast=float, policy=policy)
        assert got == 100.0
        assert not math.isnan(got)

    def test_an_int_too_large_for_a_float_is_still_a_cap(self, monkeypatch):
        """The non-finite guard must not be a ``math.isfinite`` on an int.

        ``math.isfinite(10**400)`` raises ``OverflowError`` converting to a
        float, and an uncaught one here takes down every PDF download rather
        than the absurd-but-harmless cap the operator asked for.
        """
        monkeypatch.setenv("ATM_TEST_NUM", "9" * 400)
        assert self._num() == int("9" * 400)

    def test_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("ATM_TEST_NUM", "1")
        assert self._num() == 1
        monkeypatch.setenv("ATM_TEST_NUM", "2")
        assert self._num() == 2

"""The `.claude/rules/` layer's structure, machine-checked.

Those files load only when their own ``paths:`` glob matches something a
session reads. That makes every structural defect silent: a glob orphaned by a
rename stops loading with no error anywhere, a malformed frontmatter block
loads nothing at all, and a source directory added without a topic file simply
never gets one. Prose caught none of it -- the layer has been hand-pruned twice
and re-grew in between both times.

Only structure is checked here. Form -- sentence counts, bolding, what belongs
in a docstring instead -- is judgement, and a checker for it would fail the
layer's most careful passages, including the charter's own counter-examples.
"""

import pathlib
import re

import pytest
import yaml

import academic_tools_mcp

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RULES_DIR = _ROOT / ".claude" / "rules"
_SRC = pathlib.Path(academic_tools_mcp.__file__).parent

_RULES = sorted(p.relative_to(_RULES_DIR).as_posix() for p in _RULES_DIR.rglob("*.md"))

# Source files governed by nothing but `python-design.md`, whose glob covers the
# whole package. Both are deliberate: `__init__.py` is a one-line docstring with
# no contract to state, and `fast_extract.py` is a `python -m` subprocess target
# with no importers -- the same oddity `_LAYERS` calls out in test_layering.
_UNGOVERNED = frozenset({"__init__.py", "fast_extract.py"})

# Its glob is every source file, so it co-loads with every other rules file and
# can never be the one that gives a module its topic coverage.
_BLANKET = "python-design.md"

_MAX_LINES = 200

# The charter is the one rules file that may name another. It states which globs
# are supersets of which -- the rule that decides whether a fact repeated in two
# files is waste or the only copy visible at that point of use -- and that cannot
# be said without naming the two files whose globs are the supersets.
_MAY_CITE = frozenset({"docs.md"})

# This file is the one source file that may name the rules layer, for the same
# shape of reason: it is the checker.
_MAY_NAME_THE_LAYER = frozenset({"tests/test_rules_layer.py"})


def _frontmatter(name: str) -> dict:
    """Parse one rules file's YAML frontmatter, or fail saying why it wouldn't load."""
    text = (_RULES_DIR / name).read_text()
    assert text.startswith("---\n"), f"{name}: no opening --- , so nothing is parsed as frontmatter"
    _, _, rest = text.partition("---\n")
    block, sep, _ = rest.partition("\n---")
    assert sep, f"{name}: frontmatter block is never closed"
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), (
        f"{name}: frontmatter is {type(parsed).__name__}, not a mapping"
    )
    return parsed


def _globs(name: str) -> list[str]:
    parsed = _frontmatter(name)
    paths = parsed.get("paths")
    assert isinstance(paths, list) and paths, (
        f"{name}: `paths:` must be a non-empty list, got {paths!r} -- "
        f"a rules file without one loads in every session instead of its own"
    )
    assert all(isinstance(p, str) and p.strip() for p in paths), f"{name}: empty glob in `paths:`"
    return paths


_PATTERNS = sorted((name, glob) for name in _RULES for glob in _globs(name))


def test_discovery_found_the_rules_layer():
    # Guards the scan itself, and the root: every check below walks up from
    # __file__, which is the one thing `python-design.md` forbids the package
    # from doing. Pinning a file that only exists at the root is the guard.
    assert (_ROOT / "pyproject.toml").exists(), f"{_ROOT} is not the repo root"
    assert (_ROOT / "CLAUDE.md").exists(), f"{_ROOT} is not the repo root"
    assert len(_RULES) > 15
    assert {"docs.md", _BLANKET, "providers/common.md"} <= set(_RULES)
    assert _PATTERNS


@pytest.mark.parametrize("name", _RULES)
def test_frontmatter_declares_a_usable_paths_list(name):
    _globs(name)


@pytest.mark.parametrize(("name", "glob"), _PATTERNS)
def test_every_glob_matches_at_least_one_file(name, glob):
    """A glob matching nothing is a rules file that has silently stopped loading.

    This is the layer's worst failure mode: renaming a source directory leaves
    the file in place, still correct-looking, and never loaded again.
    """
    assert next(_ROOT.glob(glob), None) is not None, (
        f"{name}: `{glob}` matches no file, so this rule never loads"
    )


@pytest.mark.parametrize(("name", "glob"), _PATTERNS)
def test_no_glob_uses_brace_expansion(name, glob):
    # `pathlib.glob` has no brace expansion, so the check above would report a
    # braced pattern as matching nothing rather than saying it can't read it.
    assert "{" not in glob, f"{name}: `{glob}` uses braces, which this checker cannot expand"


@pytest.mark.parametrize("name", _RULES)
def test_every_rules_file_stays_under_the_line_cap(name):
    lines = len((_RULES_DIR / name).read_text().splitlines())
    assert lines <= _MAX_LINES, f"{name} is {lines} lines; the cap is {_MAX_LINES}"


def test_every_source_file_has_a_topic_file():
    """Every module is covered by something narrower than the blanket file.

    `python-design.md` globs the whole package, so a naive coverage check can
    never fail. What this catches is a new directory landing with no rules file
    of its own -- inheriting only the cross-cutting rules, which is how the
    blanket file grows to hold facts that bind four modules out of forty.
    """
    covered = {
        match.resolve()
        for name in _RULES
        if name != _BLANKET
        for glob in _globs(name)
        for match in _ROOT.glob(glob)
    }
    missing = sorted(
        p.relative_to(_SRC).as_posix()
        for p in _SRC.rglob("*.py")
        if p.resolve() not in covered and p.relative_to(_SRC).as_posix() not in _UNGOVERNED
    )
    assert not missing, f"give these a topic rules file, or add them to _UNGOVERNED: {missing}"


def test_the_ungoverned_allowlist_has_not_gone_stale():
    # An allowlist naming a deleted file would quietly widen the check above.
    for name in sorted(_UNGOVERNED):
        assert (_SRC / name).exists(), f"_UNGOVERNED names {name}, which no longer exists"


@pytest.mark.parametrize("name", _RULES)
def test_no_rules_file_cites_another(name):
    """A pointer between rules files is only followable when both happen to load.

    Matched on basenames, not on the `.claude/rules` path: the charter and the
    blanket file both name the *directory* legitimately, and neither can state
    the prohibition without naming the thing prohibited.
    """
    if name in _MAY_CITE:
        pytest.skip(f"{name} is allowed to name others -- see _MAY_CITE")
    basenames = {pathlib.PurePosixPath(other).name for other in _RULES} - {
        pathlib.PurePosixPath(name).name
    }
    text = (_RULES_DIR / name).read_text()
    cited = sorted(b for b in basenames if re.search(rf"\b{re.escape(b)}", text))
    assert not cited, f"{name} cites {cited}; name the invariant instead"


def test_no_source_or_test_file_cites_the_rules_layer():
    """The same ban, from the other side -- and the only thing that states it for `tests/`.

    The charter never loads while you edit a test, and the blanket file globs
    `src/` only. Every prior violation was in `tests/`.
    """
    offenders = sorted(
        p.relative_to(_ROOT).as_posix()
        for p in [*_SRC.rglob("*.py"), *(_ROOT / "tests").rglob("*.py")]
        if ".claude" in p.read_text() and p.relative_to(_ROOT).as_posix() not in _MAY_NAME_THE_LAYER
    )
    assert not offenders, f"these cite the rules layer: {offenders}"

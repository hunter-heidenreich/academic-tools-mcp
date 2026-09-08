"""The package's layer order, machine-checked.

Every module here already obeys a strict, acyclic layer order, but until now
that order lived only in prose -- ``.claude/rules/python-design.md`` and the
README's architecture diagram. Prose does not fail CI, so a back-edge added in
good faith survives review.

This module discovers the import graph by AST (never by importing, so it is
immune to import side effects) and asserts the order directly. When a module
moves between layers, ``_LAYERS`` is the one place to edit -- and a module that
is not listed there fails ``test_every_module_is_classified`` rather than
silently escaping every check below.
"""

import ast
import pathlib

import pytest

import academic_tools_mcp

PACKAGE = academic_tools_mcp.__name__
ROOT = pathlib.Path(academic_tools_mcp.__file__).parent

# Layers in dependency order, lowest first. A module may import from its own
# layer or any lower one, never a higher one. Names are module paths relative
# to the package; a bare package name (``providers``) covers every module in it.
_LAYERS: tuple[tuple[str, frozenset[str]], ...] = (
    # The lowest layer is defined by a property, not a theme: these import
    # nothing from the package. `_fast_extract` is a `python -m` subprocess
    # target with no importers, so it stays flat rather than joining `util/`.
    ("leaf", frozenset({"util", "_fast_extract"})),
    ("net", frozenset({"net"})),
    ("store", frozenset({"store"})),
    # `download/` groups by job, not by layer, and these two really are at
    # different heights: `streaming` is the transport every provider uses,
    # while `openaccess` decides *which* URL may be fetched and so consumes
    # `openalex` and `manual`. Listed per module rather than pretending the
    # directory is one rank.
    ("download", frozenset({"download", "download.streaming"})),
    ("providers", frozenset({"providers"})),
    (
        "content",
        frozenset({"papers", "manual", "cache_search", "bibtex", "download.openaccess"}),
    ),
    ("app", frozenset({"_app"})),
    ("tools", frozenset({"tools"})),
    ("entry", frozenset({"server"})),
)

_RANK = {name: rank for rank, (_, names) in enumerate(_LAYERS) for name in names}
_LAYER_NAME = {rank: label for rank, (label, _) in enumerate(_LAYERS)}

# Only these may import httpx. The four content/provider entries need it for a
# ``_get_client() -> httpx.AsyncClient`` annotation; nobody above them does.
_MAY_IMPORT_HTTPX = frozenset(
    {"net.clients", "net.http", "net.throttle", "download.streaming", "download.openaccess"}
)


def _modules() -> dict[str, pathlib.Path]:
    """Every module in the package, keyed by its path relative to the package.

    Walks the filesystem rather than ``pkgutil`` so nothing is imported: a
    layering test that has to import the graph it is checking cannot report on
    a module whose import raises.
    """
    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue  # the package's own __init__.py
        found[".".join(parts)] = path
    return found


_MODULES = _modules()


def _resolve(name: str, node: ast.ImportFrom, alias: ast.alias) -> str | None:
    """The intra-package module an ``ImportFrom`` alias refers to, or None.

    ``from ..providers import acl`` names a module through its package, so the
    alias has to be tried as a submodule before falling back to the package
    itself (``from ..papers import Section`` is a symbol, not a module).
    """
    if node.level == 0:
        if not (node.module or "").startswith(PACKAGE):
            return None
        base = (node.module or "").removeprefix(PACKAGE).lstrip(".")
    else:
        # A package's __init__ is its own package; a plain module's is its parent.
        parts = name.split(".")
        if _MODULES[name].name != "__init__.py":
            parts = parts[:-1]
        parts = parts[: len(parts) - (node.level - 1)]
        base = ".".join([*parts, node.module] if node.module else parts)
    base = base.strip(".")
    candidate = f"{base}.{alias.name}".strip(".")
    if candidate in _MODULES:
        return candidate
    return base if base in _MODULES else None


def _is_type_checking(node: ast.AST) -> bool:
    """``if TYPE_CHECKING:`` — either spelling."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_nodes(tree: ast.AST):
    """Every node except those under an ``if TYPE_CHECKING:`` block.

    A type-only import is not a runtime edge, so it cannot create an import
    cycle and must not be reported as one: ``stats`` annotates a ``Throttle``
    it never imports at runtime, and ``throttle`` imports ``stats`` for real.
    """
    for node in ast.iter_child_nodes(tree):
        if _is_type_checking(node):
            continue
        yield node
        yield from _runtime_nodes(node)


def _imports(name: str) -> set[str]:
    """The intra-package modules ``name`` imports at runtime, by AST."""
    tree = ast.parse(_MODULES[name].read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                target = _resolve(name, node, alias)
                if target is not None and target != name:
                    out.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{PACKAGE}."):
                    target = alias.name.removeprefix(f"{PACKAGE}.")
                    if target in _MODULES and target != name:
                        out.add(target)
    return out


_GRAPH = {name: _imports(name) for name in _MODULES}


def _rank(name: str) -> int | None:
    """The layer rank of a module, matching on its package first."""
    if name in _RANK:
        return _RANK[name]
    return _RANK.get(name.split(".")[0])


def test_discovery_found_the_package():
    # Guards the scan itself: a walk that silently found nothing would make
    # every parametrized check below vacuously pass.
    assert len(_MODULES) > 30
    assert {"server", "_app", "store.cache", "providers.arxiv", "tools.paper"} <= set(_MODULES)
    assert _GRAPH["server"], "server.py imports the whole tool surface"


def test_every_module_is_classified():
    # A new module lands unclassified, which fails here rather than escaping
    # every layer check below.
    unclassified = sorted(name for name in _MODULES if _rank(name) is None)
    assert not unclassified, f"add these to _LAYERS: {unclassified}"


@pytest.mark.parametrize("name", sorted(_MODULES))
def test_module_imports_only_its_own_layer_or_lower(name):
    rank = _rank(name)
    for target in sorted(_GRAPH[name]):
        target_rank = _rank(target)
        assert target_rank <= rank, (
            f"{name} ({_LAYER_NAME[rank]}) imports {target} "
            f"({_LAYER_NAME[target_rank]}) — that is a back-edge up the stack"
        )


def test_the_lowest_layer_imports_nothing_from_the_package():
    # This is what keeps `util` from becoming a junk drawer: membership is a
    # checkable property, not a judgement call.
    lowest = sorted(n for n in _MODULES if _rank(n) == 0)
    assert len(lowest) >= 4, "the lowest layer went empty — the check would be vacuous"
    for name in lowest:
        assert not _GRAPH[name], f"{name} is in the lowest layer but imports {_GRAPH[name]}"


def test_no_import_cycles():
    # Rank ordering forbids back-edges across layers; this catches a cycle
    # *within* one, which the rank check permits (papers.index -> papers.sections
    # is a legitimate same-layer edge, a cycle between them is not).
    colour: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = 1
        for nxt in sorted(_GRAPH[node]):
            if colour.get(nxt) == 1:
                raise AssertionError(f"import cycle: {' -> '.join([*stack, node, nxt])}")
            if colour.get(nxt) is None:
                visit(nxt, [*stack, node])
        colour[node] = 2

    for name in sorted(_MODULES):
        if colour.get(name) is None:
            visit(name, [])


@pytest.mark.parametrize("name", sorted(_MODULES))
def test_httpx_stays_in_the_http_layer(name):
    source = _MODULES[name].read_text(encoding="utf-8")
    uses_httpx = any(
        (isinstance(node, ast.Import) and any(a.name == "httpx" for a in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "httpx")
        for node in ast.walk(ast.parse(source))
    )
    if uses_httpx:
        assert name in _MAY_IMPORT_HTTPX or name.startswith("providers."), (
            f"{name} imports httpx — a tool, pipeline or content module that talks "
            "HTTP directly is a layering violation; route through providers/"
        )


def test_no_provider_imports_the_conversion_pipeline():
    # This is the edge `stems` exists to keep open: three providers name a PDF
    # without importing the converter. A provider reaching `papers` re-closes it.
    for name in sorted(n for n in _MODULES if n.startswith("providers.")):
        offenders = {t for t in _GRAPH[name] if t == "papers" or t.startswith("papers.")}
        assert not offenders, f"{name} imports {offenders}; use stems for artifact naming"


def test_app_never_imports_tools():
    # The one-way edge that lets every tools/* module import _app without a cycle.
    offenders = {t for t in _GRAPH["_app"] if t == "tools" or t.startswith("tools.")}
    assert not offenders, f"_app imports {offenders} — that recreates the import cycle"


def test_no_tool_module_imports_another():
    # A helper needed by two tool modules belongs in _app, not imported sideways.
    for name in sorted(n for n in _MODULES if n.startswith("tools.")):
        offenders = {t for t in _GRAPH[name] if t.startswith("tools.")}
        assert not offenders, f"{name} imports {offenders} — move the shared helper to _app"

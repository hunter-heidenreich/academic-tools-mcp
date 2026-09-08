"""Property-based tests for the converter command builders and post-processing.

Three invariants stronger than any example set, each guarding a contract that
holds for *every* operator input rather than the ones someone wrote down:

* **The quoting trust boundary.** ``{input}`` / ``{output_dir}`` / ``{python}``
  are substituted ``shlex.quote``d so a canonical-derived path can never reach
  ``bash -c`` as code. Four hostile examples sample a rule that has to hold for
  every string an operator can type into ``import_paper``.
* **Builder totality.** Both builders must either return a ``str`` or raise
  ``ConverterTemplateError`` — the invariant that lets their callers hold the
  ``{error, retryable: False}`` contract. An enumeration of ``str.format``'s
  failure modes is exactly the thing an example suite cannot close.
* **``_IMAGE_LINK_RE``.** Its two nesting-tolerant halves exist because real
  converter output carries parentheses and brackets on both sides. Whether a
  path fragment survives into agent-visible markdown is a property of the whole
  space of captions and filenames, not of five samples.
"""

import shlex
import string
import sys
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp.papers.convert import (
    _IMAGE_LINK_RE,
    ConverterTemplateError,
    _build_converter_command,
    _build_fast_converter_command,
)
from tests.helpers.conversion_fakes import env

# Paths as an operator can really produce them: safe_stem output is tame, but
# PDF_CONVERTER_VENV and an imported local file are arbitrary operator text.
# The builders take a ``Path``, so the invariant is about the path it holds —
# ``Path("/tmp/.")`` is ``/tmp``, and normalisation is not the builders' concern.
paths = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00/"),
        min_size=1,
        max_size=30,
    )
    .map(lambda s: Path("/tmp/" + s))
    .filter(lambda q: q != Path("/tmp"))
)


# ---------------------------------------------------------------------------
# P1 — the quoting trust boundary
# ---------------------------------------------------------------------------


@given(paths, paths)
def test_full_command_never_lets_a_path_become_shell_syntax(pdf: Path, out: Path) -> None:
    """Both substituted paths survive ``bash``'s own parse as single tokens."""
    with env():
        cmd = _build_converter_command(pdf, out)
    tokens = shlex.split(cmd)
    assert str(pdf) in tokens
    assert str(out) in tokens


@given(paths)
def test_fast_command_never_lets_a_path_become_shell_syntax(pdf: Path) -> None:
    with env():
        cmd = _build_fast_converter_command(pdf)
    assert str(pdf) in shlex.split(cmd)


@given(paths)
def test_a_custom_template_gets_the_interpreter_as_one_token(pdf: Path) -> None:
    """``{python}`` is one vocabulary across both templates, both quoted."""
    with env(PDF_CONVERTER="{python} -m tool {input} -o {output_dir}"):
        full = _build_converter_command(pdf, Path("/tmp/out"))
    with env(PDF_FAST_CONVERTER="{python} -m tool {input}"):
        fast = _build_fast_converter_command(pdf)
    for cmd in (full, fast):
        tokens = shlex.split(cmd)
        assert tokens[0] == sys.executable
        assert str(pdf) in tokens


@given(paths)
def test_the_venv_activate_path_is_one_token(venv: Path) -> None:
    """``source`` must get exactly one argument, whatever the operator set."""
    with env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV=str(venv)):
        cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
    expected = str(venv.expanduser() / "bin" / "activate")
    assert expected in shlex.split(cmd)


# ---------------------------------------------------------------------------
# P2 — builder totality
# ---------------------------------------------------------------------------

# Templates built from the pieces `str.format` reacts to: braces, placeholder
# names real and wrong, positional refs, conversions and format specs.
_template_pieces = st.sampled_from(
    [
        "{input}",
        "{output_dir}",
        "{python}",
        "{nope}",
        "{0}",
        "{}",
        "{",
        "}",
        "{{",
        "}}",
        "{input!r}",
        "{input!x}",
        "{input:>10}",
        "{input:>99999999999999}",
        "{input.__class__}",
        "{input[0]}",
        "{input:{output_dir}}",
        " -o ",
        "my-tool",
    ]
)
templates = st.lists(_template_pieces, min_size=1, max_size=6).map("".join)


@given(templates)
def test_full_builder_returns_a_string_or_the_named_error(template: str) -> None:
    """Invariant: no other exception type may reach ``convert_pdf``'s caller.

    Its ``except (OSError, ConverterTemplateError)`` is the whole reason a
    malformed PDF_CONVERTER surfaces as ``{error, retryable: False}``.
    """
    with env(PDF_CONVERTER=template):
        try:
            assert isinstance(_build_converter_command(Path("/a.pdf"), Path("/o")), str)
        except ConverterTemplateError as e:
            assert "PDF_CONVERTER" in str(e)


@given(templates)
def test_fast_builder_returns_a_string_or_the_named_error(template: str) -> None:
    with env(PDF_FAST_CONVERTER=template):
        try:
            assert isinstance(_build_fast_converter_command(Path("/a.pdf")), str)
        except ConverterTemplateError as e:
            assert "PDF_FAST_CONVERTER" in str(e)


# ---------------------------------------------------------------------------
# P3 — the image-link rewrite
# ---------------------------------------------------------------------------

_plain = string.ascii_letters + string.digits + "._-"


# Captions and paths carrying *balanced* brackets/parens — the one level of
# nesting the pattern promises to tolerate, and the shape real converter output
# takes: a leaf filename derives from the PDF stem, and an Elsevier-PII DOI
# carries parentheses. An unbalanced delimiter is outside markdown's own
# grammar, so it is deliberately not generated.
def _nested(inner: str, open_ch: str, close_ch: str) -> st.SearchStrategy[str]:
    plain = st.text(alphabet=inner, min_size=0, max_size=8)
    group = st.builds(lambda t: f"{open_ch}{t}{close_ch}", plain)
    return st.lists(st.one_of(plain, group), min_size=1, max_size=3).map("".join)


captions = _nested(_plain + " ", "[", "]")
link_paths = _nested(_plain, "(", ")").map(lambda s: "images/" + s)


def _rewrite(markdown: str) -> str:
    return _IMAGE_LINK_RE.sub(r"![\1]()", markdown)


@given(captions, link_paths)
def test_a_balanced_image_link_loses_its_path_and_keeps_its_caption(
    caption: str, path: str
) -> None:
    """The path points into an extraction dir deleted on return, so no fragment
    of it may survive as body text the agent reads as content.
    """
    out = _rewrite(f"![{caption}]({path})")
    assert out == f"![{caption}]()"
    assert "images/" not in out


@given(captions, link_paths)
def test_the_rewrite_is_idempotent(caption: str, path: str) -> None:
    once = _rewrite(f"prose\n\n![{caption}]({path})\n\nmore")
    assert _rewrite(once) == once


@given(link_paths)
def test_an_ordinary_link_is_never_touched(path: str) -> None:
    """Only ``![...](...)`` is converter output; a real link may resolve."""
    markdown = f"see [the paper](https://example.org/{path})"
    assert _rewrite(markdown) == markdown

"""PDF-to-markdown conversion and section-level access.

This module handles:
  - Running a configurable PDF converter (MinerU, Marker, or custom) to produce markdown
  - Parsing markdown into sections with sub-heading previews
  - Retrieving individual sections by title or index
  - Automatic cache invalidation when markdown changes (via checksum)

The converter backend is configured via PDF_CONVERTER and PDF_CONVERTER_VENV
environment variables. See _CONVERTERS for named backends.

Section splitting is fixed, not adaptive: H1 and H2 are both treated as
section boundaries (different converters use different conventions for the
top level), H3 is tracked as the sub-heading level, and H4+ are ignored.

Cache invalidation: section indices are checksummed against the source markdown.
If the markdown file changes (e.g., manual edits), the sections are re-parsed
on the next call.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import _textnorm, atomic, cache, config

# Default subprocess timeout for PDF→markdown conversion. Big PDFs on
# CPU-only MinerU runs can legitimately take 20+ minutes, so we err
# generous. Tunable via PDF_CONVERT_TIMEOUT (seconds); "0"/"none"/"off"/
# "disabled"/any value <= 0 disables it (empty/garbage falls back here).
_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0

# Default timeout for the lightweight "fast" extraction path. Text-only
# extraction is seconds, not minutes, so the ceiling is tight. Tunable via
# PDF_FAST_CONVERT_TIMEOUT (seconds); same disable rules as the full timeout.
_DEFAULT_FAST_CONVERT_TIMEOUT = 120.0

# Global cap: at most one PDF→markdown conversion runs across the whole
# server at a time. Conversion can pin a CPU/GPU for tens of minutes;
# running multiple in parallel just thrashes resources. A second caller
# that arrives while one is already running gets a structured "busy"
# error and is expected to retry later — we deliberately do NOT queue,
# because a caller that wanted to wait could have done so itself.
_global_convert_lock = asyncio.Lock()
_current_conversion: dict[str, Any] | None = None

# Approximate tokens per character (conservative estimate for English text)
_CHARS_PER_TOKEN = 4

# Regex for heading lines: captures (level, title)
#   "# Foo"   -> (1, "Foo")
#   "## Bar"  -> (2, "Bar")
#   "### Baz" -> (3, "Baz")
# The *pattern* is the shared unit, not the compiled object: this module scans
# line by line while ``cache_search._extract_title`` scans a whole document, so
# the two need different flags but must agree on what a heading is.
HEADING_PATTERN = r"^(#{1,6})\s+(.+)$"
_HEADING_RE = re.compile(HEADING_PATTERN)

# Built-in converter command templates.
# {input} = PDF path, {output_dir} = temp extraction directory.
# {input} / {output_dir} are substituted with shlex-quoted values, so the
# templates use BARE placeholders — do NOT wrap them in quotes yourself.
_CONVERTERS: dict[str, str] = {
    "mineru": "mineru -p {input} -o {output_dir}",
    "marker": "marker_single {input} --output_dir {output_dir}",
}

# Built-in lightweight ("fast") extractor command templates. Unlike the heavy
# converters above, these emit extracted text to *stdout* (not an output dir)
# and produce plain text, not structured markdown — a deliberately degraded
# fallback. {input} = PDF path, {python} = the server's own interpreter (so the
# bundled pymupdf runner resolves against the env where the optional `[fast]`
# extra is installed).
# Like _CONVERTERS, {input} / {python} are substituted shlex-quoted — bare
# placeholders only.
_FAST_CONVERTERS: dict[str, str] = {
    "pdftotext": "pdftotext -layout {input} -",
    "pymupdf": "{python} -m academic_tools_mcp._fast_extract {input}",
}


def _busy_error(pdf_size_mb: float) -> dict[str, Any]:
    """Build the response for a caller that hit the global conversion gate.

    Tells the caller what is currently running and how long it has been
    going so an agent can decide whether to back off briefly or move on.

    The holder mutates the ``_current_conversion`` global from inside the
    lock; a follower reads it without the lock. That's safe by design:
    the read is a single atomic Python load (GIL-protected), the value
    is either a fully-populated dict or ``None``, and ``or {}`` plus
    ``.get(..., default)`` cover the cleared-but-still-locked window
    (holder finished, cleared the global, hasn't released the lock yet).
    Worst case the response says "unknown/unknown, 0s" instead of the
    just-finished work — never a crash, never a partial read.
    """
    snapshot = _current_conversion or {}
    started_at = snapshot.get("started_at")
    elapsed = (time.monotonic() - started_at) if started_at is not None else 0.0
    canonical = snapshot.get("canonical", "unknown")
    namespace = snapshot.get("namespace", "unknown")
    return {
        "error": (
            f"PDF conversion already in progress for {namespace}/{canonical} "
            f"({elapsed:.0f}s elapsed). The server runs at most one conversion "
            "at a time; retry shortly."
        ),
        "retryable": True,
        "busy": True,
        "in_progress": {
            "namespace": namespace,
            "canonical": canonical,
            "elapsed_seconds": round(elapsed, 1),
        },
        "pdf_size_mb": round(pdf_size_mb, 1),
    }


def _resolve_timeout(env_var: str, default: float) -> float | None:
    """Resolve a subprocess timeout from an env var.

    Returns the timeout in seconds, or None to disable the timeout entirely:

    - unset / empty / non-numeric ("not-a-number") / non-finite -> the default;
    - ``config._DISABLE_VALUES`` or any value <= 0 -> disabled (None);
    - a positive number -> that many seconds.

    ``on_nonpositive="disable"`` is the half that differs from
    ``MAX_PDF_BYTES``: a non-positive timeout is a second disable idiom here,
    where a non-positive size cap is a typo.
    """
    return config.number(env_var, default, cast=float, on_nonpositive="disable")


def _resolve_convert_timeout() -> float | None:
    """Resolve the full PDF conversion timeout from PDF_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_CONVERT_TIMEOUT", _DEFAULT_PDF_CONVERT_TIMEOUT)


def _resolve_fast_convert_timeout() -> float | None:
    """Resolve the fast-extraction timeout from PDF_FAST_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_FAST_CONVERT_TIMEOUT", _DEFAULT_FAST_CONVERT_TIMEOUT)


class ConverterTemplateError(ValueError):
    """A PDF_CONVERTER / PDF_FAST_CONVERTER template could not be filled in.

    ``str.format`` raises ``KeyError`` on an unknown placeholder, ``IndexError``
    on a positional one (``{0}``), and ``ValueError`` on an unbalanced brace.
    None of those is an ``OSError``, so a typo'd template escaped ``convert_pdf``
    as a raw exception instead of the ``{error, retryable: False}`` contract —
    and on the fast path the builder wasn't inside a ``try`` at all.

    Named so both call sites can catch exactly this and say something useful
    about the env var rather than surfacing a bare ``KeyError('outputdir')``.
    """


def _format_template(template: str, env_var: str, **values: str) -> str:
    """Fill in a converter command template, or raise ConverterTemplateError."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        placeholders = ", ".join(f"{{{k}}}" for k in values)
        raise ConverterTemplateError(
            f"{env_var} is not a usable command template ({e!r}). "
            f"Use bare placeholders — {placeholders} — and balance every brace; "
            "literal braces must be doubled ({{ and }})."
        ) from e


def _build_converter_command(pdf_path: Path, output_dir: Path) -> str:
    """Build the shell command for PDF-to-markdown conversion.

    Reads PDF_CONVERTER and PDF_CONVERTER_VENV from environment.
    PDF_CONVERTER can be a named backend ("mineru", "marker") or a custom
    command template containing {input} and {output_dir} placeholders. Those
    placeholders are substituted with **shell-quoted** values, so a custom
    template MUST use bare ``{input}`` / ``{output_dir}`` (not ``"{input}"``) —
    wrapping them yourself double-quotes the already-quoted value and breaks
    paths. This keeps a path with shell metacharacters from being interpreted
    by ``bash -c``.
    PDF_CONVERTER_VENV is an optional path to a virtualenv to activate first.
    """
    converter = config.get("PDF_CONVERTER") or "mineru"

    # Named backend or custom command template
    template = _CONVERTERS.get(converter, converter)
    cmd = _format_template(
        template,
        "PDF_CONVERTER",
        input=shlex.quote(str(pdf_path)),
        output_dir=shlex.quote(str(output_dir)),
    )

    # Optionally activate a venv before running
    venv = config.get("PDF_CONVERTER_VENV")
    if venv:
        activate = Path(venv).expanduser() / "bin" / "activate"
        cmd = f"source {shlex.quote(str(activate))} && {cmd}"

    return cmd


def _build_fast_converter_command(pdf_path: Path) -> str:
    """Build the shell command for lightweight ("fast") text extraction.

    Reads PDF_FAST_CONVERTER from environment. It can be a named backend
    ("pdftotext" — the default — or "pymupdf") or a custom command template
    containing an {input} placeholder (use it BARE — the value is substituted
    shell-quoted, so wrapping it in quotes yourself breaks paths). The command
    MUST emit the extracted text to stdout. {python} expands to the server's
    own interpreter so the bundled pymupdf runner resolves against the env
    where the optional `[fast]` extra is installed.
    """
    converter = config.get("PDF_FAST_CONVERTER") or "pdftotext"
    template = _FAST_CONVERTERS.get(converter, converter)
    # str.format ignores the unused {python} key for templates (e.g. pdftotext)
    # that don't reference it.
    return _format_template(
        template,
        "PDF_FAST_CONVERTER",
        input=shlex.quote(str(pdf_path)),
        python=shlex.quote(sys.executable),
    )


# A canonical id can contain characters that are unsafe in a filename or, if
# they ever reach a shell unquoted, dangerous (``$``, backtick, quotes, ...).
# ``.``/``-`` are kept so dotted DOIs and arXiv-style ids round-trip unchanged.
#
# Unsafe characters are **percent-encoded**, not collapsed to ``_``, because
# collapsing is lossy: ``"a b"`` and ``"a_b"`` would map to one ``a_b.pdf`` and
# two distinct imported papers would overwrite each other. Encoding is
# injective, so the PDF, markdown and sections paths cannot disagree about
# which file belongs to which paper.
#
# ``/`` keeps its historical ``_`` mapping rather than becoming ``%2F``: every
# DOI and old-style arXiv id contains one, so encoding it would rename
# essentially every file already on disk for no practical gain. The residual
# ambiguity (``a/b`` vs a literal ``a_b``) is unreachable for real identifiers,
# since ``/`` only appears in DOIs and old-style arXiv ids.
_SAFE_STEM_KEEP = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def safe_stem(canonical: str) -> str:
    """Map a canonical id to a filesystem/shell-safe path component.

    The single sanitizer for every derived path — PDF, markdown, and the
    sections cache key — so the three can never disagree about which file
    belongs to which paper.

    One pass, so an encoded character can never be re-encoded (a chained
    replace turned a literal ``%`` into ``%2525``).
    """
    return "".join(
        ch if ch in _SAFE_STEM_KEEP else "_" if ch == "/" else quote(ch, safe="")
        for ch in canonical
    )


# A stem that already contains only ``safe_stem`` output characters is either
# native-safe or already migrated. Re-running ``safe_stem`` on it would encode
# its own ``%`` escapes (``a%20b`` -> ``a%2520b``), so the sweep must test this
# first: ``safe_stem`` is deliberately not idempotent, the migration is.
#
# Invariant: this class is exactly ``safe_stem``'s output alphabet, so its own
# output is never seen as legacy. ``~`` belongs to it even though
# ``_SAFE_STEM_KEEP`` omits it — ``quote`` leaves the RFC 3986 unreserved set
# alone, so ``~`` passes through unencoded.
_MIGRATED_STEM_RE = re.compile(r"\A[A-Za-z0-9._%~-]*\Z")


def _needs_stem_migration(stem: str) -> bool:
    """Whether ``stem`` was written under a pre-``safe_stem`` filename rule."""
    return not _MIGRATED_STEM_RE.match(stem)


# The two artifact kinds this sweep renames; anything else in these directories
# (a temp file mid-write, an editor backup) is left alone.
_MIGRATABLE_SUFFIXES = frozenset({".pdf", ".md"})


def migrate_legacy_stems() -> int:
    """Rename cached PDFs/markdown written under the old filename rules.

    Two rules were in use before ``safe_stem``: PDFs collapsed unsafe
    characters to ``_``, markdown replaced only ``/``. Both are fixed points
    of ``safe_stem`` for ordinary arXiv ids and DOIs, so the overwhelming
    majority of an existing cache is already correct — but a DOI carrying
    parentheses (Elsevier PII style) or a freeform manual label with a space
    lands on a different name now, and would otherwise be silently orphaned:
    the paper would report "not converted yet" and re-run a conversion that
    can take tens of minutes.

    Idempotent and best-effort — a file that can't be renamed is left alone
    for the next run. Returns the number of files moved. Called once at
    server startup, alongside ``cache.gc_orphan_tmp_files``.

    The sections index is deliberately not migrated: its cache keys are
    hashed, so there is nothing to rename, and a missing index is re-derived
    from the markdown on the next read.
    """
    moved = 0
    root = cache.CACHE_ROOT
    if not root.is_dir():
        return 0
    for namespace_dir in root.iterdir():
        if not namespace_dir.is_dir():
            continue
        for entity in ("pdfs", "markdown"):
            entity_dir = namespace_dir / entity
            if not entity_dir.is_dir():
                continue
            for path in entity_dir.iterdir():
                if not path.is_file():
                    continue
                # Artifacts only. ``atomic._new_temp`` names an in-flight write
                # ``<dst.name>.<rand>.tmp``, whose stem still carries the
                # destination's legacy characters — renaming it makes the
                # writer's ``os.replace`` raise ``FileNotFoundError``.
                if path.suffix not in _MIGRATABLE_SUFFIXES:
                    continue
                if not _needs_stem_migration(path.stem):
                    continue
                target = path.with_name(safe_stem(path.stem) + path.suffix)
                if target.exists():
                    # Already migrated (or a genuine collision) — leave both
                    # in place rather than destroying data.
                    continue
                try:
                    path.rename(target)
                    moved += 1
                except OSError:
                    continue
    return moved


async def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL a converter's whole process group and reap it, best-effort.

    ``start_new_session=True`` puts the converter and anything it spawns in a
    fresh group, so killing the group takes down the tree — killing ``proc``
    alone would only kill the wrapping ``bash`` and orphan a MinerU run that
    keeps eating CPU/GPU.

    Guarded on ``returncode`` because signalling an already-reaped pid can in
    principle reach a recycled process group.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


def _make_extraction_dir(canonical: str) -> Path:
    """Create a fresh, private temp dir for converter output.

    Uses ``tempfile.mkdtemp`` (mode 0700, unguessable suffix) rather than a
    predictable ``/tmp/pdf-convert-<canonical>`` path so a hostile actor can't
    pre-create/symlink the target and multiple server instances on one host
    can't collide. The caller is responsible for removing it (``convert_pdf``
    does so in a ``finally``).
    """
    return Path(tempfile.mkdtemp(prefix=f"pdf-convert-{safe_stem(canonical)}-"))


def markdown_checksum(md_path: Path) -> str:
    """SHA-256 hex digest of a markdown file, or ``""`` if it doesn't exist.

    Used for cache invalidation — if the markdown changes, sections must be
    re-parsed. A writer that already holds the text checksums it with
    :func:`checksum_text` instead.
    """
    if not md_path.exists():
        return ""
    return hashlib.sha256(md_path.read_bytes()).hexdigest()


def checksum_text(markdown: str) -> str:
    """SHA-256 hex digest of markdown held in memory.

    Invariant: agrees with :func:`markdown_checksum` of the file
    ``atomic.write_text`` writes from the same string — that writer pins
    ``newline=""`` so the bytes on disk are exactly the UTF-8 encoding.

    A writer must checksum the string it parsed, never re-read the file it just
    wrote: the two are separated by a window in which another writer can land,
    and an index stamped with the *other* document's checksum matches disk
    forever and so is never re-parsed.
    """
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def markdown_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for converted markdown."""
    return cache.cache_dir(namespace, "markdown") / (safe_stem(canonical) + ".md")


def sections_key(canonical: str) -> str:
    """Cache key for section index JSON."""
    return safe_stem(canonical)


def drop_derived(namespace: str, canonical: str) -> None:
    """Drop a paper's converted markdown and its section index.

    The single home for the force_refresh cascade: whenever the PDF underneath
    is replaced, both halves are stale, and dropping only one leaves a reader
    matching a checksum against bytes that no longer exist.

    Caller must hold :func:`sections_lock` for the same paper — every unlinker
    of the markdown takes it. Best-effort: a file that can't be unlinked leaves
    the sections entry dropped anyway, so the next read re-parses.
    """
    with contextlib.suppress(OSError):
        markdown_path(namespace, canonical).unlink()
    cache.invalidate(namespace, "sections", sections_key(canonical))


# Per-paper async lock so two concurrent reads of the same paper don't both
# re-parse the markdown and race to write the sections cache. We cap the
# dict at ``_SECTION_LOCKS_MAX`` and evict least-recently-used first
# (OrderedDict.move_to_end on touch) so a long-running session that touches
# thousands of papers doesn't slowly grow this map without bound. Eviction
# only drops locks that are not currently held — a held lock means a
# coroutine is mid-section-cache write and dropping it would let a racing
# caller skip the serialisation we depend on.
_SECTION_LOCKS_MAX: int = 1024
_section_locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()


def sections_lock(namespace: str, canonical: str) -> asyncio.Lock:
    """Return the async lock guarding the sections cache for one paper.

    Adding/looking up under the GIL is atomic, so racing constructors
    are safe — only one Lock wins, the other is discarded uncontended.
    Touched entries move to the end so the FIFO eviction below removes
    the least-recently-used keys when the cap is exceeded.
    """
    key = (namespace, canonical)
    lock = _section_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        existing = _section_locks.setdefault(key, lock)
        if existing is lock:
            # We were the inserting writer — enforce the cap. Evict from
            # the front (oldest), skipping any lock that is currently
            # held (a held lock is doing real work right now and the
            # caller depends on its mutual exclusion) and the key we just
            # inserted (the caller is about to use it). ``held_skips``
            # counts consecutive un-evictable locks rotated to the back;
            # once it reaches the map size we've cycled past every entry
            # and none is evictable, so we bail rather than spin — going
            # slightly over cap is fine, hanging is not. Bounding the
            # probe this way keeps a full pass O(N) instead of re-scanning
            # the whole map with all(...) on every iteration.
            held_skips = 0
            while len(_section_locks) > _SECTION_LOCKS_MAX:
                if held_skips >= len(_section_locks):
                    break
                evict_key, evict_lock = next(iter(_section_locks.items()))
                if evict_key == key or evict_lock.locked():
                    _section_locks.move_to_end(evict_key)
                    held_skips += 1
                    continue
                _section_locks.pop(evict_key, None)
                held_skips = 0
        else:
            lock = existing
    _section_locks.move_to_end(key)
    return lock


# Fixed heading levels: H1 and H2 both open a new section (converters
# disagree on which level to use for the top), H3 is tracked as the
# sub-heading level, everything deeper is ignored.
SECTION_LEVELS: frozenset[int] = frozenset({1, 2})
_SUB_LEVEL: int = 3


# ---------------------------------------------------------------------------
# Section boundaries — the single home
# ---------------------------------------------------------------------------
#
# Four readers depend on these boundaries agreeing: parse_sections,
# find_in_markdown, get_section_content and cache_search.search.
# They must all come from here — a second implementation is agent-visible, not
# merely untidy. Drop the empty-section filter and a search hit names a section
# the reader's index does not have; return a title instead of an index and the
# agent's chain into get_paper_section dies on "Ambiguous section title"
# whenever a paper repeats a heading, which 10.9% of a real corpus does.


class Section:
    r"""One section's span in a markdown document.

    ``start``/``end`` are line indices into ``markdown.split("\n")``; the
    heading line itself is excluded, so ``lines[start:end]`` is the body.
    """

    __slots__ = ("end", "h3s", "start", "title")

    def __init__(self, title: str, start: int, end: int, h3s: list[str]) -> None:
        """Bind the heading title, its body span and any h3 subheadings."""
        self.title = title
        self.start = start
        self.end = end
        self.h3s = h3s

    def body(self, lines: list[str]) -> str:
        """The section's text, stripped — exactly what a reader receives."""
        return "\n".join(lines[self.start : self.end]).strip()


def _scan(markdown: str) -> tuple[list[Section], bool]:
    """The one heading scan: ``(spans, any_real_section_heading)``.

    H1 and H2 both open a section (converters disagree about which level is
    the document title), H3 is collected as a sub-heading, H4+ are ignored.
    Sections whose body is blank are dropped, so the indices returned here are
    the indices ``get_section_content`` accepts.

    Detection is returned alongside rather than recomputed, so the two callers
    that need both make one pass over the document instead of two.

    Lines are matched one at a time because ``_HEADING_RE`` is anchored with
    ``^``/``$`` and compiled without ``re.MULTILINE`` — scanning the whole
    document with it would only ever match at position 0.
    """
    lines = markdown.split("\n")
    spans: list[Section] = []
    title = "Preamble"
    start = 0
    h3s: list[str] = []
    detected = False

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if level in SECTION_LEVELS:
            detected = True
            spans.append(Section(title, start, i, h3s))
            title = m.group(2).strip()
            start = i + 1
            h3s = []
        elif level == _SUB_LEVEL:
            h3s.append(m.group(2).strip())

    spans.append(Section(title, start, len(lines), h3s))
    return [sp for sp in spans if sp.body(lines)], detected


def section_boundaries(markdown: str) -> list[Section]:
    """Split ``markdown`` into sections at H1/H2 headings. See :func:`_scan`."""
    return _scan(markdown)[0]


def has_detected_sections(markdown: str) -> bool:
    """Whether any real H1/H2 heading was found.

    A document with none collapses to a single synthetic "Preamble" section,
    which is indistinguishable from a paper that genuinely has one section
    unless callers are told. Converter output without markdown headings —
    ``pdftotext``'s layout mode, notably — hits this, and it is concentrated
    in the largest documents (theses), where navigation matters most.
    """
    return _scan(markdown)[1]


def first_section_heading(markdown: str) -> str | None:
    """The document's first H1/H2 heading text, or ``None`` if it has none.

    The single home for "what counts as the title-level heading", so a reader
    of the corpus index and a reader of the section index cannot disagree about
    which levels open a section. Not ``section_boundaries(md)[0].title``, which
    is ``"Preamble"`` for the span before the first heading.
    """
    for line in markdown.split("\n"):
        m = _HEADING_RE.match(line)
        if m is not None and len(m.group(1)) in SECTION_LEVELS:
            return m.group(2).strip()
    return None


def section_at_offset(markdown: str, offset: int) -> tuple[int, str] | None:
    """Return ``(section_index, title)`` for a character offset, or None.

    The index is the one ``get_section_content`` accepts, so a corpus-search
    hit can be chained straight into ``get_paper_section`` without going
    through an ambiguous title.
    """
    if offset < 0:
        return None
    spans = section_boundaries(markdown)
    if not spans:
        return None

    # Character offset -> line index: the count of newlines strictly before it.
    # ``str.count`` clamps its own ``end``, so no min() is needed.
    line_no = markdown.count("\n", 0, offset)
    # Spans are ordered and disjoint, so the first one ending past ``line_no``
    # is the one containing it — an explicit ``start <= line_no`` pass ahead of
    # this can never pick a different span. Offsets *between* spans (a heading
    # line) therefore resolve to the section that heading opens, except where
    # that section was dropped as empty, in which case they resolve to the next
    # surviving one. Both are indices ``get_section_content`` accepts.
    for index, sp in enumerate(spans):
        if line_no < sp.end:
            return index, sp.title
    # Past the last surviving span: a trailing empty section was filtered out.
    last = len(spans) - 1
    return last, spans[last].title


def parse_sections(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into sections with sub-heading previews.

    H1 and H2 are both treated as section boundaries; H3 is tracked as a
    sub-heading within the enclosing section. Returns a list of section dicts:
      {"index": 0, "title": "Introduction", "h3s": ["Background"], "approx_tokens": 800}

    Content before the first section heading is captured as a "Preamble" section.
    """
    return _section_dicts(markdown.split("\n"), section_boundaries(markdown))


def parse_sections_and_detect(markdown: str) -> tuple[list[dict[str, Any]], bool]:
    """:func:`parse_sections` and :func:`has_detected_sections` in one scan.

    What every writer of a sections-cache entry wants: the entry carries both,
    and computing them separately walks the document twice.
    """
    spans, detected = _scan(markdown)
    return _section_dicts(markdown.split("\n"), spans), detected


def _section_dicts(lines: list[str], spans: list[Section]) -> list[dict[str, Any]]:
    """Render scanned spans as the agent-facing section index."""
    return [
        {
            "index": index,
            "title": sp.title,
            "h3s": list(sp.h3s),
            # Measured on the stripped body — exactly what get_section_content
            # returns and counts, so the index and the reader agree.
            "approx_tokens": max(1, len(sp.body(lines)) // _CHARS_PER_TOKEN),
        }
        for index, sp in enumerate(spans)
    ]


# Snippet window around an in-paper match. ~60 chars on each side gives
# the agent enough context to recognise relevance without overflowing.
_FIND_SNIPPET_WINDOW = 60


def find_in_markdown(
    markdown: str,
    query: str,
    *,
    max_results: int = 20,
    case_sensitive: bool = False,
    whole_words: bool = False,
    normalize: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    r"""Scan markdown for occurrences of ``query`` and return per-hit context.

    Each hit carries the section title, section index (matching what
    ``get_paper_section`` exposes), the character offset within that
    section's stripped text (so an agent can call
    ``get_paper_section(identifier, section_index, offset=char_offset)``
    to land at the match), and a ~120-char snippet centred on the match.

    ``whole_words=True`` wraps the query in ``\\b…\\b`` so "set" doesn't
    match "subset". ``case_sensitive=False`` is the default — academic
    prose capitalisation is unreliable.

    ``normalize=True`` NFKD-folds the query and each section's text and
    strips combining marks before matching, so "cafe" matches "café" and
    "Gutierrez" matches "Gutiérrez" (and vice versa). Offsets, ``match``,
    and ``snippet`` are still sliced from the ORIGINAL (un-folded) text —
    a fold-with-position-map translates each match back to original
    offsets — so chaining into ``get_paper_section`` still lands on the
    match. Caveat: ``\\b`` word boundaries are ASCII-oriented; folding
    turns diacritic Latin words into ASCII so ``whole_words`` works for
    them, but non-Latin scripts (CJK, Arabic) stay unreliable for
    ``whole_words`` and are largely unaffected by folding. A query that
    matches only part of one original character's expansion (``"f"`` inside
    a "ﬁ" ligature) reports the whole original character as ``match``.

    Hit offsets align with ``get_paper_section``'s stripped section text
    because both apply the same ``"\\n".join(lines[s:e]).strip()`` recipe.

    Returns ``(hits, truncated)`` where ``truncated`` is ``True`` when the
    scan stopped at ``max_results`` with more matches still in the document,
    so callers can signal "more exist" instead of silently capping.
    """
    if not query:
        return [], False

    lines = markdown.split("\n")
    boundaries = [(sp.title, sp.start, sp.end) for sp in section_boundaries(markdown)]

    if normalize:
        folded_query = _textnorm.fold(query)
        if not folded_query:
            # Query was entirely combining marks — an empty pattern would
            # match at every position, so there is nothing to find.
            return [], False
        pattern = re.escape(folded_query)
    else:
        pattern = re.escape(query)
    if whole_words:
        pattern = rf"\b{pattern}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    hits: list[dict[str, Any]] = []
    for section_index, (title, start, end) in enumerate(boundaries):
        # Same recipe as get_section_content so offsets align.
        section_text = "\n".join(lines[start:end]).strip()
        # When normalising, match against the folded text but keep a map
        # back to original offsets so char_offset/match/snippet stay
        # aligned with the un-folded section text get_section_content
        # returns.
        if normalize:
            search_text, index_map = _textnorm.fold_with_map(section_text)
        else:
            search_text, index_map = section_text, None
        for match in regex.finditer(search_text):
            if len(hits) >= max_results:
                return hits, True
            if index_map is None:
                pos = match.start()
                matched = match.group()
            else:
                pos, span_end = _textnorm.original_span(index_map, match.start(), match.end())
                matched = section_text[pos:span_end]
            ws = max(0, pos - _FIND_SNIPPET_WINDOW)
            we = min(len(section_text), pos + len(matched) + _FIND_SNIPPET_WINDOW)
            # Collapse newlines so the snippet renders on one line in
            # the agent's view; the surrounding context stays readable.
            snippet = section_text[ws:we].replace("\n", " ").strip()
            hits.append(
                {
                    "section_index": section_index,
                    "section": title,
                    "char_offset": pos,
                    "match": matched,
                    "snippet": snippet,
                }
            )
    return hits, False


def _match_section_title(
    section: str, boundaries: list[tuple[str, int, int]]
) -> list[tuple[int, str, int, int]]:
    """Sections whose title contains ``section``, case- and diacritic-insensitively.

    The folded pass runs only when the exact one finds nothing, so folding can
    widen a miss into a hit ("Resume" → "Résumé") but never turns a resolving
    query into an ambiguity error.
    """
    query = section.lower()
    matches = [(i, t, s, e) for i, (t, s, e) in enumerate(boundaries) if query in t.lower()]
    if matches:
        return matches
    folded = _textnorm.fold(section).lower()
    return [
        (i, t, s, e)
        for i, (t, s, e) in enumerate(boundaries)
        if folded in _textnorm.fold(t).lower()
    ]


def get_section_content(
    markdown: str,
    section: int | str,
    offset: int = 0,
    max_chars: int = 16000,
) -> dict[str, Any]:
    """Retrieve a slice of a section's content by index or title.

    Args:
        markdown: Full markdown text.
        section: Integer index, or a string title matched as a case-insensitive
            substring — falling back to a diacritic-folded comparison when
            nothing matches exactly (see ``_match_section_title``).
        offset: Starting character offset within the section. Defaults to 0.
            Use ``next_offset`` from a previous call to page through.
        max_chars: Slice size in characters. Defaults to 16000 (~4000 tokens).
            Must be positive.

    Returns:
        On success: ``{index, title, content, offset, chars_returned,
        total_chars, approx_tokens, has_more, next_offset}``. ``approx_tokens``
        and ``total_chars`` describe the full section, not the slice.
        On error: ``{"error": ...}`` (lists available titles for unknown
        section names).
    """
    if max_chars <= 0:
        return {"error": f"max_chars must be positive, got {max_chars}"}
    if offset < 0:
        return {"error": f"offset must be non-negative, got {offset}"}

    lines = markdown.split("\n")

    boundaries = [(sp.title, sp.start, sp.end) for sp in section_boundaries(markdown)]

    if not boundaries:
        # A converter can exit 0 having produced an empty or whitespace-only
        # markdown file (a 0-page PDF, an image-only scan). convert_paper then
        # reports success with sections: [], and every read here fell through
        # to the range check below and produced the literal, useless
        # "out of range (0--1)". Say what actually happened instead.
        return {
            "error": "The converted markdown for this paper is empty — no readable text.",
            "suggestion": (
                "The PDF probably has no extractable text layer (a scan or an "
                "image-only document). Re-run convert_paper with force_refresh=True "
                "to try again, or check the PDF opens and contains selectable text."
            ),
            "retryable": False,
        }

    if isinstance(section, int):
        if 0 <= section < len(boundaries):
            resolved_index = section
            title, start, end = boundaries[section]
        else:
            return {"error": f"Section index {section} out of range (0-{len(boundaries) - 1})"}
    else:
        matches = _match_section_title(section, boundaries)
        if len(matches) == 1:
            resolved_index, title, start, end = matches[0]
        elif len(matches) > 1:
            titles = [t for _, t, _, _ in matches]
            return {"error": f"Ambiguous section title '{section}'. Matches: {titles}"}
        else:
            titles = [t for t, _, _ in boundaries]
            return {"error": f"No section matching '{section}'. Available: {titles}"}

    full_content = "\n".join(lines[start:end]).strip()
    total_chars = len(full_content)
    approx_tokens = max(1, total_chars // _CHARS_PER_TOKEN)

    if offset > total_chars:
        return {"error": f"offset {offset} is beyond section length {total_chars}"}

    end_offset = min(offset + max_chars, total_chars)
    slice_content = full_content[offset:end_offset]
    has_more = end_offset < total_chars

    return {
        "index": resolved_index,
        "title": title,
        "content": slice_content,
        "offset": offset,
        "chars_returned": len(slice_content),
        "total_chars": total_chars,
        "approx_tokens": approx_tokens,
        "has_more": has_more,
        "next_offset": end_offset if has_more else None,
    }


def _read_markdown(md_path: Path) -> str | None:
    """Read cached markdown as UTF-8, or ``None`` if it isn't there.

    One read, no ``exists()`` ahead of it: the check-then-read has a window a
    concurrent ``drop_derived`` fits through, and the answer is the same either
    way.
    """
    try:
        return md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


async def _reparse_sections_locked(
    namespace: str,
    canonical: str,
    md_path: Path,
    *,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Return the sections payload for a converted paper, re-parsing if stale.

    **The caller MUST already hold the per-paper ``sections_lock``** (this is
    the shared core behind ``get_or_parse_sections`` and ``convert_pdf``'s
    cached-markdown branch, which both hold the lock for the surrounding work).

    Returns ``{sections, markdown_checksum, conversion_mode}`` or ``None`` when
    the markdown is missing — covering both "never converted" and the race where
    a concurrent ``force_refresh`` cascade unlinks the file after an ``exists()``
    check (every unlinker holds this same lock, so a successful read means the
    file is stable for the rest of this call).

    The read and the re-parse each run off the event loop, and the read is
    explicit UTF-8 so a non-UTF-8 host locale can't mis-decode. The checksum
    comes from the text that was read, not a second pass over the file, so it
    and the parsed sections always describe the same bytes.
    """
    if force_refresh:
        cache.invalidate(namespace, "sections", sections_key(canonical))

    text = await asyncio.to_thread(_read_markdown, md_path)
    if text is None:
        return None
    current_checksum = checksum_text(text)

    cached = cache.get(namespace, "sections", sections_key(canonical))
    if cached is not None:
        stored_checksum = cached.get("markdown_checksum")
        if (
            stored_checksum is not None
            and stored_checksum == current_checksum
            and cached.get("sections") is not None
            # An entry predating ``sections_detected`` is re-parsed rather than
            # read with a guessed default. Re-parsing is a regex pass over text
            # already in hand — no subprocess, no network — so computing the
            # true answer is cheaper than the cost of reporting a wrong one,
            # which is an agent told a heading-free thesis "has one section".
            and cached.get("sections_detected") is not None
        ):
            return cached

    # No/stale sections cache (or a legacy entry missing the parsed sections) —
    # re-parse and refresh, preserving any recorded conversion_mode: a re-parse
    # produces no new evidence about what converted the file.
    recorded_mode = cached.get("conversion_mode") if cached is not None else None

    sections, detected = await asyncio.to_thread(parse_sections_and_detect, text)
    payload = {
        "sections": sections,
        "sections_detected": detected,
        "markdown_checksum": current_checksum,
        "conversion_mode": recorded_mode,
    }
    cache.put(namespace, "sections", sections_key(canonical), payload)
    return payload


async def get_or_parse_sections(
    namespace: str, canonical: str, *, force_refresh: bool = False
) -> dict[str, Any] | None:
    """Public sections accessor: read the cache, re-parsing when it drifted.

    Re-parses the markdown if the section index is missing or its checksum
    no longer matches. Acquires the per-paper ``sections_lock`` and delegates to
    ``_reparse_sections_locked``. Returns the sections payload
    (``{sections, markdown_checksum, conversion_mode}``) or ``None`` when the
    paper isn't converted (no markdown on disk). ``force_refresh=True`` drops
    the cached section index first so the next read re-parses.
    """
    md_path = markdown_path(namespace, canonical)
    async with sections_lock(namespace, canonical):
        return await _reparse_sections_locked(
            namespace, canonical, md_path, force_refresh=force_refresh
        )


def store_markdown_and_index(
    namespace: str,
    canonical: str,
    md_path: Path,
    markdown: str,
    mode: str,
) -> dict[str, Any]:
    """Write markdown to the cache and store its section index.

    The single home for assembling a sections-cache entry. Every writer routes
    through here — the two conversion modes via :func:`_finalize_markdown`, and
    ``manual.import_markdown`` directly — so the payload can never be assembled
    with a key missing.

    Invariant: a sections-cache entry always carries all four of ``sections``,
    ``sections_detected``, ``markdown_checksum`` and ``conversion_mode``.
    ``manual.import_markdown`` wrote only two of them, and since
    ``_reparse_sections_locked`` accepts an entry whose checksum matches, an
    imported heading-free paper was reported to the agent as
    ``sections_detected: true`` — the exact reading ``sections_note`` exists to
    prevent. (Guarded by tests/test_manual.py::TestImportMarkdown::
    test_cached_sections_carry_every_key_a_conversion_writes.)

    ``mode`` is the provenance tag: ``"full"`` / ``"fast"`` for converter
    output, ``"imported"`` for a pre-converted file that never ran through one.

    Takes the markdown verbatim — post-processing belongs to the caller, since
    what is right for converter output (see :func:`_finalize_markdown`) is
    wrong for a file the operator wrote by hand.
    """
    # Atomic UTF-8 write: a crash mid-write can't leave a torn markdown file,
    # and non-ASCII content survives a non-UTF-8 host locale.
    atomic.write_text(md_path, markdown)

    sections, detected = parse_sections_and_detect(markdown)
    cache.put(
        namespace,
        "sections",
        sections_key(canonical),
        {
            "sections": sections,
            "sections_detected": detected,
            "markdown_checksum": checksum_text(markdown),
            "conversion_mode": mode,
        },
    )
    return {
        "markdown_path": str(md_path),
        "sections": sections,
        "sections_detected": detected,
        "cached": False,
        "conversion_mode": mode,
    }


# ``![caption](path)``, tolerating one level of nesting on each side.
#
# Both halves are load-bearing against real converter output. A flat
# ``\([^)]*\)`` stops at the first ``)`` *inside* the path, so
# ``![cap](fig(1).png)`` rewrites to ``![cap]().png)`` — the tail becomes body
# text the agent reads as content. Converter leaf filenames derive from the PDF
# stem, and an Elsevier-PII DOI carries parentheses. A flat ``\[([^\]]*)\]``
# likewise skips ``![a [b] c](path)`` entirely, leaving a dead extraction-dir
# path in agent-visible markdown.
_IMAGE_LINK_RE = re.compile(r"!\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:[^()]|\([^()]*\))*\)")


def _finalize_markdown(
    namespace: str,
    canonical: str,
    md_path: Path,
    raw_markdown: str,
    mode: str,
) -> dict[str, Any]:
    """Post-process converter output, then store it via the shared writer.

    Shared tail for both conversion modes ("full" and "fast"). The
    post-processing here is specific to *converter* output and deliberately not
    part of :func:`store_markdown_and_index`: an imported markdown file is the
    operator's own text, and rewriting its image links would be data loss.
    """
    # Normalise trailing whitespace line-by-line.
    markdown = "\n".join(line.rstrip() for line in raw_markdown.split("\n"))

    # Strip unused image paths: ``![caption](path)`` → ``![caption]()``. The path
    # points into the extraction temp dir, removed as soon as the conversion
    # returns, so it can never resolve; the caption is kept.
    markdown = _IMAGE_LINK_RE.sub(r"![\1]()", markdown)

    return store_markdown_and_index(namespace, canonical, md_path, markdown, mode)


async def _convert_fast(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    pdf_size_mb: float,
) -> dict[str, Any]:
    """Lightweight text extraction, run *outside* the global conversion lock.

    Shells out to PDF_FAST_CONVERTER (default ``pdftotext``) capturing stdout,
    then caches the text as markdown via the shared finaliser. Deliberately
    degraded: plain text, no tables/equations/figures, no real headings. Cheap
    and not GPU-bound, so it never serialises behind a heavy MinerU conversion
    and never returns a ``busy`` error. The per-paper sections lock serialises
    concurrent fast calls on the same paper so they don't both spawn and race
    the cache write; stderr is captured separately so stdout stays clean text.
    """
    md_path = markdown_path(namespace, canonical)
    async with sections_lock(namespace, canonical):
        # A racing fast caller may have written the markdown between the outer
        # cached-check and our acquiring this lock — re-check before spawning.
        # The shared re-parser returns None when the file is gone (a concurrent
        # force_refresh cascade unlinked it), so we fall through and extract.
        # Going through it rather than assembling an entry here is what keeps
        # ``conversion_mode`` honest: it preserves a recorded mode and leaves a
        # legacy ``null`` alone, where a local ``recorded_mode or "fast"``
        # stamps a paper nobody has evidence about as degraded.
        cached = await _reparse_sections_locked(namespace, canonical, md_path)
        if cached is not None:
            return {
                "markdown_path": str(md_path),
                "sections": cached["sections"],
                "sections_detected": cached["sections_detected"],
                "cached": True,
                "conversion_mode": cached.get("conversion_mode"),
            }

        try:
            cmd = _build_fast_converter_command(pdf_path)
        except ConverterTemplateError as e:
            # Invariant: a malformed PDF_FAST_CONVERTER surfaces as
            # {error, retryable: False}, never a raised exception. The builder
            # must stay inside this try.
            return {
                "error": str(e),
                "retryable": False,
                "conversion_mode": "fast",
            }
        timeout = _resolve_fast_convert_timeout()

        try:
            # start_new_session=True so a timeout can SIGKILL the whole tree.
            # stderr is kept separate (not merged) so stdout is pure text.
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            return {
                "error": (
                    f"Could not start fast PDF extractor subprocess: {e}. "
                    "Check that the PDF_FAST_CONVERTER command is installed "
                    "(default 'pdftotext' needs poppler-utils; 'pymupdf' needs "
                    "`pip install academic-tools-mcp[fast]`)."
                ),
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        try:
            if timeout is None:
                stdout, stderr = await proc.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            # Client disconnect / tool-call cancellation / server shutdown.
            # The `finally` below cleans up the extraction dir and the lock but
            # does **not** signal the child — without this kill a converter run
            # keeps pinning CPU/GPU with its output directory deleted underneath
            # it, and is never reaped. Kill, then re-raise: cancellation is not
            # an error to report to the caller.
            await _kill_process_group(proc)
            raise
        except TimeoutError:
            await _kill_process_group(proc)
            return {
                "error": (
                    f"Fast PDF extraction timed out after {timeout:.0f}s "
                    f"(PDF: {pdf_size_mb:.1f} MB). "
                    "Increase PDF_FAST_CONVERT_TIMEOUT or set it to 'none' to disable."
                ),
                "retryable": False,
                "timed_out": True,
                "timeout_seconds": timeout,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        if proc.returncode != 0:
            # Prefer stderr (where extractors write diagnostics); fall back to
            # stdout. Replace undecodable bytes rather than raising.
            output = (stderr or b"").decode("utf-8", errors="replace") or (stdout or b"").decode(
                "utf-8", errors="replace"
            )
            return {
                "error": f"Fast PDF extraction failed (exit {proc.returncode}): {output[-500:]}",
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        markdown = (stdout or b"").decode("utf-8", errors="replace")
        # pdftotext separates pages with a form-feed; turn it into a blank line.
        markdown = markdown.replace("\f", "\n")
        if not markdown.strip():
            return {
                "error": (
                    f"Fast PDF extractor produced no text (PDF: {pdf_size_mb:.1f} MB). "
                    "The PDF may be image-only/scanned — try full conversion (MinerU "
                    "runs OCR) instead."
                ),
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        return await asyncio.to_thread(
            _finalize_markdown, namespace, canonical, md_path, markdown, "fast"
        )


async def convert_pdf(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    *,
    force_refresh: bool = False,
    mode: str = "full",
) -> dict[str, Any]:
    """Convert a PDF to markdown, cache the result, and return section index.

    Args:
        pdf_path: Path to the cached PDF file.
        namespace: Cache namespace (e.g., "arxiv").
        canonical: Canonical ID for cache keying.
        force_refresh: If True, drop any cached markdown + section index
            for this paper so the converter re-runs. Use after replacing the
            source PDF or upgrading the converter.
        mode: ``"full"`` (default) runs the heavy converter (MinerU/Marker)
            under the global single-conversion lock. ``"fast"`` runs a
            lightweight text extractor (PDF_FAST_CONVERTER, default
            ``pdftotext``) outside that lock — a deliberately degraded path
            that never serialises or returns ``busy``, useful when the full
            converter times out or is unavailable.

    Returns:
        Dict with markdown_path, sections, cached, conversion_mode, or an error.
    """
    md_path = markdown_path(namespace, canonical)

    if force_refresh:
        # Drop both halves under the per-paper lock so a concurrent reader
        # can't catch a half-cleared state (markdown gone, stale sections
        # entry still pointing at the old checksum).
        async with sections_lock(namespace, canonical):
            drop_derived(namespace, canonical)

    # If the markdown is already cached, never re-run the slow conversion —
    # re-parse from the existing markdown if the sections cache is missing or
    # stale (handled by the shared _reparse_sections_locked, which also returns
    # None if the file vanished under the lock so we fall through to conversion).
    if md_path.exists():
        async with sections_lock(namespace, canonical):
            payload = await _reparse_sections_locked(namespace, canonical, md_path)
            if payload is not None:
                return {
                    "markdown_path": str(md_path),
                    "sections": payload["sections"],
                    # Reported on every call, not just the first. A fresh
                    # conversion has always carried this; the cached branch
                    # dropped it, so the response shape changed once the
                    # markdown was on disk.
                    "sections_detected": payload["sections_detected"],
                    "cached": True,
                    "conversion_mode": payload.get("conversion_mode"),
                }

    if not pdf_path.exists():
        return {"error": f"PDF not found at: {pdf_path}"}

    # Report PDF size so callers can gauge feasibility
    pdf_size_bytes = pdf_path.stat().st_size
    pdf_size_mb = pdf_size_bytes / (1024 * 1024)

    # Fast path: lightweight extraction outside the global lock. Never
    # serialises behind a heavy conversion and never returns a busy error.
    if mode == "fast":
        return await _convert_fast(pdf_path, namespace, canonical, pdf_size_mb)

    # Global single-conversion gate. The check-then-acquire is safe
    # because asyncio.Lock.acquire() on an uncontended lock returns
    # without yielding — no other coroutine can sneak in between
    # `if locked()` and `async with`.
    if _global_convert_lock.locked():
        return _busy_error(pdf_size_mb)

    async with _global_convert_lock:
        global _current_conversion  # noqa: PLW0603 — the gate is process-wide by design
        _current_conversion = {
            "namespace": namespace,
            "canonical": canonical,
            "started_at": time.monotonic(),
        }
        # Bound before the try so the finally can always clean it up, even if
        # subprocess setup throws before the assignment below.
        extract_dir: Path | None = None
        try:
            timeout = _resolve_convert_timeout()

            try:
                # Fresh private temp dir (mkdtemp, 0700) — no predictable path
                # to pre-create/symlink, no cross-instance collision, so no
                # `rm -rf` pre-step is needed. start_new_session=True puts the
                # converter (and any children it spawns) into a fresh process
                # group so we can SIGKILL the whole tree on timeout. Without it,
                # killing `proc` only kills bash and orphans the converter,
                # which keeps eating CPU/GPU.
                extract_dir = _make_extraction_dir(canonical)
                converter_cmd = _build_converter_command(pdf_path, extract_dir)
                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    "-c",
                    converter_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except (OSError, ConverterTemplateError) as e:
                # Setup failed: a malformed PDF_CONVERTER template, temp-dir
                # creation, or process spawn (bash missing, fork EAGAIN,
                # perms). Different from a converter that ran and failed.
                return {
                    "error": (
                        f"Could not start PDF converter subprocess: {e}. "
                        "Check that bash is on PATH and that the PDF_CONVERTER / "
                        "PDF_CONVERTER_VENV env vars point at a usable command."
                    ),
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            try:
                if timeout is None:
                    stdout, stderr = await proc.communicate()
                else:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.CancelledError:
                # Client disconnect / tool-call cancellation / server shutdown.
                # The `finally` below rmtree's the extraction dir and releases
                # the global conversion lock but does **not** signal the child —
                # without this kill a MinerU run keeps pinning CPU/GPU with its
                # output directory deleted underneath it, and is never reaped.
                # Kill, then re-raise: cancellation is not an error to report.
                await _kill_process_group(proc)
                raise
            except TimeoutError:
                # Take down the whole process group, then give it a moment to
                # actually exit before we return so we don't leave zombies.
                await _kill_process_group(proc)
                return {
                    "error": (
                        f"PDF conversion timed out after {timeout:.0f}s "
                        f"(PDF: {pdf_size_mb:.1f} MB). "
                        "Increase PDF_CONVERT_TIMEOUT or set it to 'none' to disable."
                    ),
                    "retryable": False,
                    "timed_out": True,
                    "timeout_seconds": timeout,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            if proc.returncode != 0:
                # Invariant: stderr is captured on its own pipe and appended
                # last, so a converter that logs progress to stdout cannot push
                # its real error out of the 500-char tail. Never merge the two
                # with `2>&1`. (Guarded by
                # tests/test_failure_modes.py::TestConverterSubprocessPlumbing.)
                #
                # Undecodable bytes are replaced rather than raising: a crashing
                # converter can emit binary noise.
                output = (stdout or b"").decode("utf-8", errors="replace") + (stderr or b"").decode(
                    "utf-8", errors="replace"
                )
                return {
                    "error": f"PDF conversion failed (exit {proc.returncode}): {output[-500:]}",
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            # Find the generated markdown file in the output directory
            stem = pdf_path.stem
            # Sorted: glob order is filesystem-dependent, and MinerU emits
            # several .md files per run — so which one *became* the paper was
            # nondeterministic whenever the exact-stem match missed.
            candidates = sorted(extract_dir.glob(f"**/{stem}.md"))

            if not candidates:
                # Try any .md file in the output
                # Deterministic fallback: shallowest path first, then by name,
                # so a top-level output beats one nested in a subdirectory.
                candidates = sorted(
                    extract_dir.glob("**/*.md"),
                    key=lambda q: (len(q.relative_to(extract_dir).parts), str(q)),
                )

            if not candidates:
                return {
                    "error": f"PDF converter produced no markdown output (PDF: {pdf_size_mb:.1f} MB).",
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            source_md = candidates[0]

            # Read + post-process + write + parse in one worker hop: a
            # thesis-sized markdown is megabytes of regex and hashing, and the
            # event loop is serving every other tool call meanwhile.
            def _read_and_finalize() -> dict[str, Any]:
                raw = source_md.read_text(encoding="utf-8")
                return _finalize_markdown(namespace, canonical, md_path, raw, "full")

            return await asyncio.to_thread(_read_and_finalize)
        finally:
            # Clean up the temp extraction dir on every exit — success *and*
            # all four failure paths (spawn error, timeout, non-zero exit,
            # no-markdown) — so failed conversions don't leak /tmp dirs.
            if extract_dir is not None:
                shutil.rmtree(extract_dir, ignore_errors=True)
            _current_conversion = None

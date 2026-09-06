"""Running a PDF converter and turning its output into cached markdown.

Two modes share one cache slot, so a later ``mode="full"`` + ``force_refresh``
upgrades a fast conversion:

* **full** — the heavy converter (MinerU/Marker, ``PDF_CONVERTER``) under a
  global single-conversion gate. A second concurrent caller gets a structured
  ``busy`` error rather than queueing.
* **fast** — a lightweight stdout-capturing text extractor
  (``PDF_FAST_CONVERTER``) outside that gate. Deliberately degraded: plain
  text, no tables, equations, figures or real headings.

Every backend is spawned through ``bash -c`` with shlex-quoted substitutions, so
a template carries **bare** ``{input}`` / ``{output_dir}`` / ``{python}``
placeholders — quoting them yourself double-quotes an already-quoted value.
"""

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

from .. import config
from .._stems import markdown_path, safe_stem
from .index import (
    _reparse_sections_locked,
    drop_derived,
    sections_lock,
    store_markdown_and_index,
)

# Default subprocess timeout for PDF→markdown conversion. Big PDFs on
# CPU-only MinerU runs can legitimately take 20+ minutes, so we err
# generous. Tunable via PDF_CONVERT_TIMEOUT (seconds); "0"/"none"/"off"/
# "disabled"/any value <= 0 disables it (empty/garbage falls back here).
_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0

# Default timeout for the lightweight "fast" extraction path. Text-only
# extraction is seconds, not minutes, so the ceiling is tight. Tunable via
# PDF_FAST_CONVERT_TIMEOUT (seconds); same disable rules as the full timeout.
_DEFAULT_FAST_CONVERT_TIMEOUT = 120.0

# At most one full conversion server-wide: it can pin a CPU/GPU for tens of
# minutes, and running several just thrashes. A second caller gets a structured
# "busy" error rather than queueing — one that wanted to wait could have.
_global_convert_lock = asyncio.Lock()
_current_conversion: dict[str, Any] | None = None


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

    Says what is running and for how long, so an agent can decide whether to
    back off briefly or move on.

    The unlocked read of ``_current_conversion`` is safe: it is a single
    GIL-protected load of either a fully-populated dict or ``None``, and the
    defaults cover the cleared-but-still-locked window. Worst case the response
    says "unknown/unknown, 0s" — never a crash, never a partial read.
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
    on a positional one (``{0}``), and ``ValueError`` on an unbalanced brace —
    none of them an ``OSError``. Narrowing them to one named error is what lets
    both builders' callers hold the ``{error, retryable: False}`` contract and
    name the env var, rather than surfacing a bare ``KeyError('outputdir')``.

    Invariant: both builders stay inside their caller's ``try``.
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


class _Completed(NamedTuple):
    """A converter that ran to completion, however it exited."""

    stdout: bytes
    stderr: bytes
    returncode: int


class _SpawnFailed(NamedTuple):
    """The subprocess never started: bash missing, fork EAGAIN, permissions."""

    error: OSError


class _TimedOut(NamedTuple):
    """The converter overran its budget and its process group was killed."""

    timeout: float


_RunOutcome = _Completed | _SpawnFailed | _TimedOut


async def _run_command(cmd: str, timeout_seconds: float | None) -> _RunOutcome:
    """Run a converter under ``bash -c`` and capture both streams.

    The one home for the control flow both conversion modes need, so a change
    to the cancellation or timeout discipline cannot land in one and miss the
    other. What each mode says *about* an outcome stays with that mode: the
    messages differ, and so does how the two streams are combined.

    ``start_new_session=True`` puts the converter and anything it spawns in a
    fresh process group, so a timeout can SIGKILL the tree rather than just the
    wrapping ``bash`` and orphan a MinerU run that keeps eating CPU/GPU.

    stdout and stderr are kept on separate pipes and never merged with
    ``2>&1``: the fast path captures stdout as the document, and the full path
    appends stderr last so a chatty converter cannot push its real error out of
    the truncated tail.

    On cancellation — client disconnect, tool-call cancellation, shutdown — the
    tree is killed and ``CancelledError`` re-raised. Neither caller's
    ``finally`` signals the child, so without this a converter keeps running
    with its output directory deleted underneath it and is never reaped.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        return _SpawnFailed(e)

    try:
        if timeout_seconds is None:
            stdout, stderr = await proc.communicate()
        else:
            # Nested so the timeout stays a ``float`` in the handler that
            # reports it — there is no TimeoutError without a wait_for.
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                await _kill_process_group(proc)
                return _TimedOut(timeout_seconds)
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise

    return _Completed(stdout or b"", stderr or b"", proc.returncode or 0)


def _decode(raw: bytes) -> str:
    """Decode converter output, replacing undecodable bytes.

    A crashing converter can emit binary noise, and that must reach the agent
    as a truncated message rather than a UnicodeDecodeError.
    """
    return raw.decode("utf-8", errors="replace")


def _make_extraction_dir(canonical: str) -> Path:
    """Create a fresh, private temp dir for converter output.

    ``mkdtemp`` (mode 0700, unguessable suffix) rather than a predictable
    ``/tmp/pdf-convert-<canonical>``, which invites a symlink or pre-creation
    attack and collides across instances. The caller removes it in a ``finally``.
    """
    return Path(tempfile.mkdtemp(prefix=f"pdf-convert-{safe_stem(canonical)}-"))


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
        outcome = await _run_command(cmd, _resolve_fast_convert_timeout())
        failed = {
            "retryable": False,
            "conversion_mode": "fast",
            "pdf_size_mb": round(pdf_size_mb, 1),
        }

        if isinstance(outcome, _SpawnFailed):
            return {
                "error": (
                    f"Could not start fast PDF extractor subprocess: {outcome.error}. "
                    "Check that the PDF_FAST_CONVERTER command is installed "
                    "(default 'pdftotext' needs poppler-utils; 'pymupdf' needs "
                    "`pip install academic-tools-mcp[fast]`)."
                ),
                **failed,
            }

        if isinstance(outcome, _TimedOut):
            return {
                "error": (
                    f"Fast PDF extraction timed out after {outcome.timeout:.0f}s "
                    f"(PDF: {pdf_size_mb:.1f} MB). "
                    "Increase PDF_FAST_CONVERT_TIMEOUT or set it to 'none' to disable."
                ),
                "timed_out": True,
                "timeout_seconds": outcome.timeout,
                **failed,
            }

        if outcome.returncode != 0:
            # Prefer stderr, where extractors write diagnostics; stdout is the
            # document channel and may be empty on failure.
            output = _decode(outcome.stderr) or _decode(outcome.stdout)
            return {
                "error": (
                    f"Fast PDF extraction failed (exit {outcome.returncode}): {output[-500:]}"
                ),
                **failed,
            }

        markdown = _decode(outcome.stdout)
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
                    # Invariant: the response shape is the same cached or
                    # fresh, so an agent never feature-detects.
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

            failed = {"retryable": False, "pdf_size_mb": round(pdf_size_mb, 1)}

            try:
                extract_dir = _make_extraction_dir(canonical)
                converter_cmd = _build_converter_command(pdf_path, extract_dir)
            except (OSError, ConverterTemplateError) as e:
                # Setup failed: a malformed PDF_CONVERTER template or temp-dir
                # creation. Distinct from a converter that ran and failed.
                return {
                    "error": (
                        f"Could not start PDF converter subprocess: {e}. "
                        "Check that bash is on PATH and that the PDF_CONVERTER / "
                        "PDF_CONVERTER_VENV env vars point at a usable command."
                    ),
                    **failed,
                }

            outcome = await _run_command(converter_cmd, timeout)

            if isinstance(outcome, _SpawnFailed):
                return {
                    "error": (
                        f"Could not start PDF converter subprocess: {outcome.error}. "
                        "Check that bash is on PATH and that the PDF_CONVERTER / "
                        "PDF_CONVERTER_VENV env vars point at a usable command."
                    ),
                    **failed,
                }

            if isinstance(outcome, _TimedOut):
                return {
                    "error": (
                        f"PDF conversion timed out after {outcome.timeout:.0f}s "
                        f"(PDF: {pdf_size_mb:.1f} MB). "
                        "Increase PDF_CONVERT_TIMEOUT or set it to 'none' to disable."
                    ),
                    "timed_out": True,
                    "timeout_seconds": outcome.timeout,
                    **failed,
                }

            if outcome.returncode != 0:
                # Invariant: stderr is appended *last*, so a converter that logs
                # progress to stdout cannot push its real error out of the
                # 500-char tail. (Guarded by
                # tests/test_failure_modes.py::TestConverterSubprocessPlumbing.)
                output = _decode(outcome.stdout) + _decode(outcome.stderr)
                return {
                    "error": f"PDF conversion failed (exit {outcome.returncode}): {output[-500:]}",
                    **failed,
                }

            # Find the generated markdown file in the output directory
            stem = pdf_path.stem
            # Sorted: glob order is filesystem-dependent and MinerU emits
            # several .md files per run, so an unsorted pick is nondeterministic
            # about which one becomes the paper.
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

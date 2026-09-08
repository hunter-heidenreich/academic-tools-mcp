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

# Generous: a CPU-only MinerU run on a big PDF legitimately takes 20+ minutes.
_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0

# Tight: text-only extraction is seconds, not minutes.
_DEFAULT_FAST_CONVERT_TIMEOUT = 120.0

# One full conversion server-wide: it can pin a CPU/GPU for tens of minutes,
# and running several just thrashes.
_global_convert_lock = asyncio.Lock()
_current_conversion: dict[str, Any] | None = None


# Built-in PDF_CONVERTER backends.
_CONVERTERS: dict[str, str] = {
    "mineru": "mineru -p {input} -o {output_dir}",
    "marker": "marker_single {input} --output_dir {output_dir}",
}

# Built-in PDF_FAST_CONVERTER backends. {python} points the bundled pymupdf
# runner at the env where the optional `[fast]` extra is installed.
_FAST_CONVERTERS: dict[str, str] = {
    "pdftotext": "pdftotext -layout {input} -",
    "pymupdf": "{python} -m academic_tools_mcp._fast_extract {input}",
}


def _busy_error(pdf_size_mb: float) -> dict[str, Any]:
    """Build the response for a caller that hit the global conversion gate.

    Says what is running and for how long, so an agent can decide whether to
    back off briefly or move on.

    Reads ``_current_conversion`` unlocked, once: nothing here awaits, so the
    snapshot cannot change underfoot. The defaults hold the answer to
    "unknown/unknown, 0s" rather than a crash if it is ever read cleared.
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
        "conversion_mode": "full",
        "in_progress": {
            "namespace": namespace,
            "canonical": canonical,
            "elapsed_seconds": round(elapsed, 1),
        },
        "pdf_size_mb": round(pdf_size_mb, 1),
    }


def _resolve_timeout(env_var: str, default: float) -> float | None:
    """Resolve a subprocess timeout from an env var: seconds, or None for no timeout."""
    return config.number(env_var, default, cast=float, on_nonpositive="disable")


def _resolve_convert_timeout() -> float | None:
    """Resolve the full PDF conversion timeout from PDF_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_CONVERT_TIMEOUT", _DEFAULT_PDF_CONVERT_TIMEOUT)


def _resolve_fast_convert_timeout() -> float | None:
    """Resolve the fast-extraction timeout from PDF_FAST_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_FAST_CONVERT_TIMEOUT", _DEFAULT_FAST_CONVERT_TIMEOUT)


class ConverterTemplateError(ValueError):
    """A PDF_CONVERTER / PDF_FAST_CONVERTER template could not be filled in.

    Every way ``str.format`` can fail on operator text becomes this one named
    error — an open set, so don't re-narrow the ``except`` to the few you can
    name. Invariant: both builders stay inside their caller's ``try``, which is
    what holds the ``{error, retryable: False}`` contract.
    """


def _format_template(template: str, env_var: str, **values: str) -> str:
    """Fill in a converter command template, or raise ConverterTemplateError."""
    try:
        return template.format(**values)
    except Exception as e:
        placeholders = ", ".join(f"{{{k}}}" for k in values)
        raise ConverterTemplateError(
            f"{env_var} is not a usable command template ({e!r}). "
            f"Use bare placeholders — {placeholders} — and balance every brace; "
            "literal braces must be doubled ({{ and }})."
        ) from e


def _setup_error(detail: object) -> str:
    """The message for a full conversion that never reached the converter."""
    return (
        f"Could not start PDF converter subprocess: {detail}. "
        "Check that bash is on PATH and that the PDF_CONVERTER / "
        "PDF_CONVERTER_VENV env vars point at a usable command."
    )


def _build_converter_command(pdf_path: Path, output_dir: Path) -> str:
    """Build the shell command for PDF-to-markdown conversion.

    ``PDF_CONVERTER`` is a named backend (``_CONVERTERS``) or a custom template;
    ``PDF_CONVERTER_VENV`` optionally names a virtualenv to activate first.
    ``{python}`` is this server's interpreter, so a converter installed beside
    it can be run without ``PDF_CONVERTER_VENV`` at all.
    """
    converter = config.get("PDF_CONVERTER") or "mineru"

    template = _CONVERTERS.get(converter, converter)
    cmd = _format_template(
        template,
        "PDF_CONVERTER",
        input=shlex.quote(str(pdf_path)),
        output_dir=shlex.quote(str(output_dir)),
        python=shlex.quote(sys.executable),
    )

    venv = config.get("PDF_CONVERTER_VENV")
    if venv:
        activate = Path(venv).expanduser() / "bin" / "activate"
        cmd = f"source {shlex.quote(str(activate))} && {cmd}"

    return cmd


def _build_fast_converter_command(pdf_path: Path) -> str:
    """Build the shell command for lightweight ("fast") text extraction.

    ``PDF_FAST_CONVERTER`` is a named backend (``_FAST_CONVERTERS``, default
    ``pdftotext``) or a custom template. Whichever it is, the command **must**
    emit the extracted text to stdout and its diagnostics to stderr.
    """
    converter = config.get("PDF_FAST_CONVERTER") or "pdftotext"
    template = _FAST_CONVERTERS.get(converter, converter)
    return _format_template(
        template,
        "PDF_FAST_CONVERTER",
        input=shlex.quote(str(pdf_path)),
        python=shlex.quote(sys.executable),
    )


async def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL a converter's whole process group and reap it, best-effort.

    The group, not ``proc``: killing the wrapper alone orphans a MinerU run that
    keeps eating CPU/GPU. Guarded on ``returncode`` because signalling an
    already-reaped pid can in principle reach a recycled group.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(OSError):
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

    The one subprocess driver, so cancellation and timeout discipline cannot
    diverge between the two modes; each mode still words its own outcomes.

    ``start_new_session=True`` so a kill reaches the whole tree, not just the
    wrapping ``bash``. The streams stay on separate pipes — never ``2>&1`` —
    because the fast path captures stdout as the document. Cancellation kills
    the tree here and re-raises: neither caller's ``finally`` signals the child.
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

    # ``returncode`` is ``int | None``; after communicate() it is set, and a
    # default beats an assertion that could fire in production.
    return _Completed(stdout or b"", stderr or b"", proc.returncode or 0)


def _decode(raw: bytes) -> str:
    """Decode converter output; a crashing converter can emit binary noise."""
    return raw.decode("utf-8", errors="replace")


def _shallowest_first(extract_dir: Path, pattern: str) -> list[Path]:
    """Glob converter output shallowest-first, then by name.

    MinerU emits several ``.md`` files per run and glob order is
    filesystem-dependent, so both candidate passes share this one ordering.
    """
    return sorted(
        extract_dir.glob(pattern),
        key=lambda q: (len(q.relative_to(extract_dir).parts), str(q)),
    )


def _make_extraction_dir(canonical: str) -> Path:
    """Create a fresh, private temp dir for converter output.

    ``mkdtemp`` rather than a predictable ``/tmp/pdf-convert-<canonical>``,
    which invites a symlink attack and collides across instances. The caller
    removes it in a ``finally``.
    """
    return Path(tempfile.mkdtemp(prefix=f"pdf-convert-{safe_stem(canonical)}-"))


# ``![caption](path)``, tolerating one level of nesting on each side — both
# halves are load-bearing against real converter output. A flat ``\([^)]*\)``
# stops at the first ``)`` inside the path (``![cap](fig(1).png)`` leaves
# ``.png)`` behind as body text), and a flat ``\[([^\]]*)\]`` skips
# ``![a [b] c](path)`` entirely, leaving a dead path in agent-visible markdown.
_IMAGE_LINK_RE = re.compile(r"!\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:[^()]|\([^()]*\))*\)")


def _cached_response(md_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Shape a ``_reparse_sections_locked`` payload as a conversion response.

    Invariant: the same keys as :func:`store_markdown_and_index` returns, so an
    agent never feature-detects between a cached and a fresh conversion.
    ``conversion_mode`` is ``.get``: an entry predating the field answers
    ``null``, and nobody may guess on its behalf.
    """
    return {
        "markdown_path": str(md_path),
        "sections": payload["sections"],
        "sections_detected": payload["sections_detected"],
        "cached": True,
        "conversion_mode": payload.get("conversion_mode"),
    }


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
    markdown = "\n".join(line.rstrip() for line in raw_markdown.split("\n"))

    # Image paths point into the extraction dir, deleted on return, so they can
    # never resolve. The caption is kept.
    markdown = _IMAGE_LINK_RE.sub(r"![\1]()", markdown)

    return store_markdown_and_index(namespace, canonical, md_path, markdown, mode)


async def _convert_fast(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    pdf_size_mb: float,
) -> dict[str, Any]:
    """Lightweight text extraction, run *outside* the global conversion lock.

    Cheap and not GPU-bound, so it never queues behind a heavy conversion and
    can never return ``busy``. Its only serialisation is the per-paper sections
    lock, which keeps two callers on one paper from both spawning.
    """
    md_path = markdown_path(namespace, canonical)
    async with sections_lock(namespace, canonical):
        # A racing caller may have written the markdown since the outer check.
        # Going through the shared re-parser rather than assembling an entry
        # here is what keeps ``conversion_mode`` honest; None means the file is
        # gone, so fall through and extract.
        cached = await _reparse_sections_locked(namespace, canonical, md_path)
        if cached is not None:
            return _cached_response(md_path, cached)

        failed = {
            "retryable": False,
            "conversion_mode": "fast",
            "pdf_size_mb": round(pdf_size_mb, 1),
        }

        try:
            cmd = _build_fast_converter_command(pdf_path)
        except ConverterTemplateError as e:
            # Invariant: a malformed PDF_FAST_CONVERTER surfaces as
            # {error, retryable: False}, never a raised exception. The builder
            # must stay inside this try.
            return {"error": str(e), **failed}
        outcome = await _run_command(cmd, _resolve_fast_convert_timeout())

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
        # A form-feed page break becomes a line break (the pdftotext
        # convention; harmless for a backend that emits none).
        markdown = markdown.replace("\f", "\n")
        if not markdown.strip():
            return {
                "error": (
                    f"Fast PDF extractor produced no text (PDF: {pdf_size_mb:.1f} MB). "
                    "The PDF may be image-only/scanned — try full conversion (MinerU "
                    "runs OCR) instead."
                ),
                **failed,
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
        mode: ``"full"`` (default) or ``"fast"`` — see the module docstring.

    Returns:
        Dict with markdown_path, sections, cached, conversion_mode, or an error.
    """
    if mode not in ("full", "fast"):
        # The MCP boundary types this Literal, so only a direct library caller
        # gets here — and a typo must not silently start a 20-minute run.
        return {
            "error": f"Unknown conversion mode {mode!r}. Use 'full' or 'fast'.",
            "retryable": False,
        }

    md_path = markdown_path(namespace, canonical)

    if force_refresh:
        # Both halves under one lock, so a reader can't catch a half-cleared
        # state: markdown gone, sections entry still on the old checksum.
        async with sections_lock(namespace, canonical):
            drop_derived(namespace, canonical)

    # Cached markdown never re-runs the converter; a missing or stale sections
    # entry only costs a re-parse. None means the file vanished under the lock,
    # so fall through and convert.
    if md_path.exists():
        async with sections_lock(namespace, canonical):
            payload = await _reparse_sections_locked(namespace, canonical, md_path)
            if payload is not None:
                return _cached_response(md_path, payload)

    # One stat, no exists() ahead of it: the check-then-stat has a window a
    # concurrent unlink fits through, and the answer is the same either way.
    # Deliberately unnamed: no cache filesystem path crosses the MCP boundary,
    # and _strip_internal_paths only drops path-valued *keys*.
    try:
        pdf_size_bytes = pdf_path.stat().st_size
    except OSError:
        return {"error": "PDF not found in the cache.", "retryable": False}

    # Reported on every error from here down, so callers can gauge feasibility.
    pdf_size_mb = pdf_size_bytes / (1024 * 1024)

    if mode == "fast":
        return await _convert_fast(pdf_path, namespace, canonical, pdf_size_mb)

    # Check-then-acquire is safe: acquiring an uncontended asyncio.Lock returns
    # without yielding, so nothing can slip between these two statements.
    if _global_convert_lock.locked():
        return _busy_error(pdf_size_mb)

    async with _global_convert_lock:
        global _current_conversion  # noqa: PLW0603 — the gate is process-wide by design
        _current_conversion = {
            "namespace": namespace,
            "canonical": canonical,
            "started_at": time.monotonic(),
        }
        # Bound before the try so the finally can clean up a setup that threw.
        extract_dir: Path | None = None
        try:
            failed = {
                "retryable": False,
                "conversion_mode": "full",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

            try:
                extract_dir = _make_extraction_dir(canonical)
                converter_cmd = _build_converter_command(pdf_path, extract_dir)
            except (OSError, ConverterTemplateError) as e:
                # A bad template or an unwritable temp dir — distinct from a
                # converter that ran and failed.
                return {"error": _setup_error(e), **failed}

            outcome = await _run_command(converter_cmd, _resolve_convert_timeout())

            if isinstance(outcome, _SpawnFailed):
                return {"error": _setup_error(outcome.error), **failed}

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
                # Invariant: stderr last, so a converter that logs progress to
                # stdout can't push its real error out of the 500-char tail.
                output = _decode(outcome.stdout) + _decode(outcome.stderr)
                return {
                    "error": f"PDF conversion failed (exit {outcome.returncode}): {output[-500:]}",
                    **failed,
                }

            # Prefer a file named after the PDF, else any .md.
            stem = pdf_path.stem
            candidates = _shallowest_first(extract_dir, f"**/{stem}.md") or _shallowest_first(
                extract_dir, "**/*.md"
            )

            if not candidates:
                return {
                    "error": f"PDF converter produced no markdown output (PDF: {pdf_size_mb:.1f} MB).",
                    **failed,
                }

            source_md = candidates[0]

            # One worker hop for read + rewrite + write + parse: a thesis is
            # megabytes of regex and hashing, and the loop is serving other calls.
            def _read_and_finalize() -> dict[str, Any]:
                raw = source_md.read_text(encoding="utf-8")
                return _finalize_markdown(namespace, canonical, md_path, raw, "full")

            return await asyncio.to_thread(_read_and_finalize)
        finally:
            # Every exit path, so a failed conversion can't leak a /tmp dir.
            if extract_dir is not None:
                shutil.rmtree(extract_dir, ignore_errors=True)
            _current_conversion = None

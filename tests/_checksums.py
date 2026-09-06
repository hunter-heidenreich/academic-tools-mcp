"""The on-disk half of the sections-index checksum invariant.

``_stems.checksum_text`` is what every writer stamps into the index; this is
what the bytes on disk actually hash to. Only the suites need both, so the
oracle lives here — a production path that reaches for it is the bug
``.claude/rules/pipeline.md`` § Checksums describes.
"""

import hashlib
from pathlib import Path


def markdown_checksum(md_path: Path) -> str:
    """SHA-256 hex digest of a markdown file. Raises if it isn't there.

    Strict on purpose: a sentinel for "missing" would let an assertion against
    a mistyped path pass by comparing two empty strings.
    """
    return hashlib.sha256(md_path.read_bytes()).hexdigest()

"""The on-disk half of the sections-index checksum invariant.

``stems.checksum_text`` is what every writer stamps into the index; this is
what the bytes on disk actually hash to. Only the suites need both, so the
oracle lives here: a writer must checksum the string it parsed, never the file
it just wrote, so a production path that reaches for this is the bug.
"""

import hashlib
from pathlib import Path


def markdown_checksum(md_path: Path) -> str:
    """SHA-256 hex digest of a markdown file. Raises if it isn't there.

    Strict on purpose: a sentinel for "missing" would let an assertion against
    a mistyped path pass by comparing two empty strings.
    """
    return hashlib.sha256(md_path.read_bytes()).hexdigest()

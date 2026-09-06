"""Artifact naming: the stem sanitizer, its checksum helpers, and the sweep.

Covers ``_stems``. The sanitizer is the only reason two papers cannot share a
file, and the sweep runs unattended at startup over a cache the operator cannot
easily repair.
"""

import re
from pathlib import Path

import pytest

from academic_tools_mcp import _stems, cache, papers

from ._checksums import markdown_checksum


def _seed(tmp_path, namespace, entity, names):
    """Write cached artifacts under ``<tmp_path>/<namespace>/<entity>/``."""
    d = tmp_path / namespace / entity
    d.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if entity == "pdfs" else ".md"
    for n in names:
        (d / (n + suffix)).write_text("x")
    return d


class TestSafeStem:
    """``safe_stem`` maps a canonical id to a filesystem/shell-safe stem."""

    def test_strips_shell_metacharacters(self):
        stem = papers.safe_stem('x"$(touch pwned)`id`;rm -rf /|y')
        # Unsafe characters are percent-encoded rather than collapsed, so the
        # stem stays injective — but nothing shell-significant survives.
        assert re.fullmatch(r"[A-Za-z0-9._%-]+", stem)
        for bad in ('"', "$", "(", ")", "`", ";", "|", " ", "/"):
            assert bad not in stem

    def test_is_injective_for_identifiers_that_used_to_collide(self):
        # "a b" and "a_b" both mapped to "a_b", so importing one silently
        # overwrote the other's cached PDF.
        assert papers.safe_stem("a b") != papers.safe_stem("a_b")
        assert papers.safe_stem("some:label") != papers.safe_stem("some_label")

    def test_percent_is_not_double_encoded(self):
        assert papers.safe_stem("x%y") == "x%25y"

    def test_encoding_its_own_output_would_double_encode(self):
        # safe_stem is deliberately NOT idempotent — "%" is itself encoded.
        # This is why the migration sweep gates on _needs_stem_migration
        # rather than comparing safe_stem(stem) to stem.
        assert papers.safe_stem("a%20b") == "a%2520b"

    def test_needs_stem_migration_only_for_legacy_names(self):
        assert _stems._needs_stem_migration("10.1016_s1-6(03)02831-9") is True
        assert _stems._needs_stem_migration("my paper") is True
        # Already-safe and already-migrated names are left alone.
        assert _stems._needs_stem_migration("2301.00001") is False
        assert _stems._needs_stem_migration("10.1101_2021.01.01.123") is False
        assert _stems._needs_stem_migration("a%20b") is False
        # "~" is unreserved, so `quote` passes it through: it is safe_stem
        # output, not a legacy name. Read as legacy, the sweep re-encodes the
        # "%" beside it and orphans the file it just renamed.
        assert _stems._needs_stem_migration("notes~draft%202024") is False

    def test_safe_stem_output_is_never_seen_as_legacy(self):
        """Invariant: the migration gate accepts everything safe_stem emits.

        Otherwise startup renames a correctly-written file to a stem the path
        builder never produces, and the paper reports "not converted yet".
        """
        for canonical in [
            "notes~draft 2024",
            "10.1038/s41586-024-00001-1",
            "hep-th/9901001v2",
            'x"$(touch pwned)`id`;rm -rf /|y',
            "Grüße/Übung 1",
        ]:
            stem = papers.safe_stem(canonical)
            assert _stems._needs_stem_migration(stem) is False

    def test_a_literal_percent_2f_is_not_read_as_an_encoded_slash(self):
        """The ``%2F`` rewrite is exact, not a heuristic.

        ``safe_stem`` percent-encodes first and maps ``%2F`` back to ``_``. A
        literal ``%`` becomes ``%25``, so the only way that escape reaches the
        output is a real ``/`` — otherwise the two ids below would share a file.
        """
        assert papers.safe_stem("a/b") == "a_b"
        assert papers.safe_stem("a%2Fb") == "a%252Fb"

    def test_keeps_normal_doi_and_arxiv_ids(self):
        # Normal identifiers are unchanged except for the legacy / -> _ map,
        # so existing cache filenames don't churn.
        assert papers.safe_stem("10.1101/2021.01.01.123") == "10.1101_2021.01.01.123"
        assert papers.safe_stem("2301.00001v2") == "2301.00001v2"
        assert papers.safe_stem("hep-th/9901001") == "hep-th_9901001"


class TestMigrateLegacyStems:
    """Renames cached files written under the pre-``safe_stem`` filename rules.

    Without this a DOI carrying parentheses or a manual label with a space
    would be silently orphaned by the new scheme: the paper reports "not
    converted yet" and re-runs a conversion that can take tens of minutes.
    """

    def test_renames_legacy_names(self, tmp_path):
        d = _seed(tmp_path, "manual", "markdown", ["10.1016_s0304-3975(03)00229-9"])

        moved = papers.migrate_legacy_stems()

        assert moved == 1
        assert (d / "10.1016_s0304-3975%2803%2900229-9.md").exists()
        assert not (d / "10.1016_s0304-3975(03)00229-9.md").exists()

    def test_leaves_ordinary_identifiers_alone(self, tmp_path):
        names = ["2301.00001", "10.1101_2021.01.01.123", "hep-th_9901001", "P16-1160"]
        d = _seed(tmp_path, "arxiv", "pdfs", names)

        assert papers.migrate_legacy_stems() == 0
        assert sorted(p.stem for p in d.iterdir()) == sorted(names)

    def test_is_idempotent(self, tmp_path):
        _seed(tmp_path, "manual", "pdfs", ["my paper"])

        first = papers.migrate_legacy_stems()
        second = papers.migrate_legacy_stems()

        assert (first, second) == (1, 0), "a second sweep must not re-encode"
        assert (tmp_path / "manual" / "pdfs" / "my%20paper.pdf").exists()

    def test_does_not_clobber_an_existing_target(self, tmp_path):
        d = _seed(tmp_path, "manual", "pdfs", ["a b", "a%20b"])

        assert papers.migrate_legacy_stems() == 0, "must not overwrite a real file"
        assert (d / "a b.pdf").exists() and (d / "a%20b.pdf").exists()

    def test_missing_cache_root_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "does-not-exist")
        assert papers.migrate_legacy_stems() == 0

    def test_ignores_unrelated_entities(self, tmp_path):
        _seed(tmp_path, "openalex", "works", ["some(thing)"])
        assert papers.migrate_legacy_stems() == 0

    def test_counts_every_namespace_and_both_entity_dirs(self, tmp_path):
        """The count is the whole sweep, not the first directory it finds."""
        _seed(tmp_path, "manual", "pdfs", ["a paper"])
        _seed(tmp_path, "manual", "markdown", ["a paper"])
        _seed(tmp_path, "arxiv", "pdfs", ["another paper"])

        assert papers.migrate_legacy_stems() == 3

    def test_ignores_unrelated_suffixes_in_an_artifact_dir(self, tmp_path):
        # Only .pdf/.md are artifacts. An editor backup or a stray sidecar
        # carries the destination's legacy characters and is not ours to move.
        d = tmp_path / "manual" / "pdfs"
        d.mkdir(parents=True)
        strays = ["my paper.pdf.bak", "my paper.json", "my paper"]
        for name in strays:
            (d / name).write_text("x")

        assert papers.migrate_legacy_stems() == 0
        assert sorted(p.name for p in d.iterdir()) == sorted(strays)


class TestMigrateLegacyStemsSkips:
    """What the sweep must leave alone. It runs at startup over directories
    other writers are using, and every rename it gets wrong orphans a file or
    breaks a write in flight.
    """

    def test_a_temp_file_mid_write_is_not_renamed(self, tmp_path):
        # ``atomic._new_temp`` names an in-flight write
        # ``<dst.name>.<rand>.tmp``, whose stem still carries the destination's
        # legacy characters. Renaming it makes the writer's os.replace raise
        # FileNotFoundError and lose the download.
        d = tmp_path / "manual" / "pdfs"
        d.mkdir(parents=True)
        live = d / "my paper.pdf.a1b2c3.tmp"
        live.write_text("x")

        assert papers.migrate_legacy_stems() == 0
        assert live.exists()

    def test_a_non_directory_namespace_entry_is_skipped(self, tmp_path):
        (tmp_path / "stray-file").write_text("x")
        assert papers.migrate_legacy_stems() == 0

    def test_a_subdirectory_inside_an_entity_dir_is_skipped(self, tmp_path):
        # Named like a legacy artifact, so it clears the suffix and stem checks
        # and only the is_file() guard stands between it and a rename.
        nested = tmp_path / "manual" / "markdown" / "a dir.md"
        nested.mkdir(parents=True)
        assert papers.migrate_legacy_stems() == 0
        assert nested.is_dir()

    def test_an_unreadable_directory_does_not_stop_the_server(self, tmp_path, monkeypatch):
        """The sweep runs inside the startup lifespan, so it may not raise.

        An ``iterdir`` that fails on permissions used to propagate out of
        ``_lifespan`` and leave the server unable to start over a cache the
        operator could have ignored.
        """
        _seed(tmp_path, "manual", "markdown", ["my paper"])

        def refuse(self):
            raise PermissionError("cache is not readable")

        monkeypatch.setattr(Path, "iterdir", refuse)
        assert papers.migrate_legacy_stems() == 0

    def test_one_unreadable_namespace_does_not_hide_the_rest(self, tmp_path, monkeypatch):
        _seed(tmp_path, "manual", "markdown", ["my paper"])
        blocked = tmp_path / "arxiv" / "pdfs"
        blocked.mkdir(parents=True)
        real_iterdir = Path.iterdir

        def refuse(self):
            if self == blocked:
                raise PermissionError("not readable")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", refuse)
        assert papers.migrate_legacy_stems() == 1

    def test_a_rename_that_fails_leaves_the_file_for_the_next_run(self, tmp_path, monkeypatch):
        d = tmp_path / "manual" / "markdown"
        d.mkdir(parents=True)
        legacy = d / "my paper.md"
        legacy.write_text("x")

        def refuse(self, target):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "rename", refuse)
        assert papers.migrate_legacy_stems() == 0
        assert legacy.exists()


class TestSectionsKeyForStem:
    """The sections key for a paper already named by a stem on disk.

    The identity, and it has to stay one: re-sanitizing would encode the stem's
    own ``%`` escapes, so ``manual.migrate_misrouted_arxiv`` would invalidate a
    key nothing was ever stored under and leave the stale index in place.
    """

    def test_agrees_with_sections_key_for_the_identifier_that_named_the_file(self):
        for canonical in ["2301.00001", "10.1016/s1-6(03)02831-9", "notes~draft 2024", "a/b"]:
            stem = papers.safe_stem(canonical)
            assert _stems.sections_key_for_stem(stem) == papers.sections_key(canonical)

    def test_re_sanitizing_a_stem_would_not_agree(self):
        stem = papers.safe_stem("my paper")
        assert papers.safe_stem(stem) != _stems.sections_key_for_stem(stem)


class TestMarkdownPathForStem:
    def test_agrees_with_markdown_path_for_the_identifier_that_named_the_file(self):
        """One naming rule, whether the caller holds the id or the stem.

        ``cache_search`` reads a hit's file from a stem recovered off disk; a
        second spelling of the recipe there would read a path nothing wrote.
        """
        for canonical in ["2301.00001", "10.1016/s1-6(03)02831-9", "notes~draft 2024"]:
            stem = papers.safe_stem(canonical)
            assert papers.markdown_path_for_stem("ns", stem) == papers.markdown_path(
                "ns", canonical
            )


class TestChecksumText:
    def test_an_empty_document_still_has_a_checksum(self, tmp_path):
        """Absent and empty are different states, and only one has a digest.

        An index entry for a paper whose markdown is a zero-byte file must
        still match disk; a sentinel shared with "no file" would make the two
        compare equal and suppress the re-parse that heals it.
        """
        empty = tmp_path / "empty.md"
        empty.write_text("")
        assert markdown_checksum(empty) == papers.checksum_text("")

        with pytest.raises(OSError):
            markdown_checksum(tmp_path / "nope.md")

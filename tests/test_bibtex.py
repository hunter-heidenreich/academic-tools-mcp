from academic_tools_mcp.bibtex import (
    _extract_last_name,
    _format_authors_bibtex,
    _generate_key,
    generate_bibtex,
)


class TestExtractLastName:
    def test_simple(self):
        assert _extract_last_name("John Smith") == "smith"

    def test_particle(self):
        assert _extract_last_name("Derek van Tilborg") == "vantilborg"

    def test_multiple_particles(self):
        assert _extract_last_name("Maria de la Cruz") == "delacruz"

    def test_single_name(self):
        assert _extract_last_name("Madonna") == "madonna"

    def test_accented(self):
        assert _extract_last_name("François Müller") == "muller"

    def test_middle_name(self):
        assert _extract_last_name("Andrew J. Ballard") == "ballard"


class TestFormatAuthorsBibtex:
    def test_single_author(self):
        authorships = [{"author": {"display_name": "John Smith"}}]
        assert _format_authors_bibtex(authorships) == "Smith, John"

    def test_multiple_authors(self):
        authorships = [
            {"author": {"display_name": "John Smith"}},
            {"author": {"display_name": "Jane Doe"}},
        ]
        assert _format_authors_bibtex(authorships) == "Smith, John and Doe, Jane"

    def test_particle_author(self):
        authorships = [{"author": {"display_name": "Derek van Tilborg"}}]
        assert _format_authors_bibtex(authorships) == "van Tilborg, Derek"

    def test_empty_name_skipped(self):
        authorships = [
            {"author": {"display_name": "John Smith"}},
            {"author": {"display_name": ""}},
        ]
        assert _format_authors_bibtex(authorships) == "Smith, John"

    def test_three_part_particle(self):
        authorships = [{"author": {"display_name": "Ludwig van den Berg"}}]
        assert _format_authors_bibtex(authorships) == "van den Berg, Ludwig"


class TestGenerateKey:
    def test_standard(self):
        work = {
            "authorships": [{"author": {"display_name": "John Smith"}}],
            "publication_year": 2022,
            "title": "A Novel Approach to Testing",
        }
        assert _generate_key(work) == "smith2022novel"

    def test_particle_author(self):
        work = {
            "authorships": [{"author": {"display_name": "Derek van Tilborg"}}],
            "publication_year": 2022,
            "title": "Exposing the Limitations",
        }
        assert _generate_key(work) == "vantilborg2022exposing"

    def test_no_authors(self):
        work = {
            "authorships": [],
            "publication_year": 2022,
            "title": "Some Paper",
        }
        assert _generate_key(work) == "unknown2022some"

    def test_skips_articles_in_title(self):
        work = {
            "authorships": [{"author": {"display_name": "Jane Doe"}}],
            "publication_year": 2021,
            "title": "The Art of Programming",
        }
        assert _generate_key(work) == "doe2021art"

    def test_accented_author_produces_ascii_key(self):
        work = {
            "authorships": [{"author": {"display_name": "Augustin Žídek"}}],
            "publication_year": 2021,
            "title": "Protein Folding",
        }
        key = _generate_key(work)
        assert key == "zidek2021protein"
        assert key.isascii()


class TestGenerateBibtex:
    def _make_work(self, **overrides):
        base = {
            "type": "article",
            "title": "Test Paper",
            "publication_year": 2022,
            "doi": "https://doi.org/10.1234/test",
            "authorships": [
                {"author": {"display_name": "John Smith"}, "institutions": []}
            ],
            "biblio": {
                "volume": "1",
                "issue": "2",
                "first_page": "10",
                "last_page": "20",
            },
            "primary_location": {
                "source": {
                    "display_name": "Test Journal",
                    "host_organization_name": "Test Publisher",
                }
            },
            "ids": {},
        }
        base.update(overrides)
        return base

    def test_article(self):
        bib = generate_bibtex(self._make_work())
        assert bib.startswith("@article{smith2022test,")
        assert "journal={Test Journal}" in bib
        assert "volume={1}" in bib
        assert "number={2}" in bib
        assert "pages={10--20}" in bib
        assert "year={2022}" in bib
        assert "doi={10.1234/test}" in bib

    def test_preprint(self):
        bib = generate_bibtex(self._make_work(type="preprint"))
        assert bib.startswith("@misc{")
        assert "journal=" not in bib

    def test_inproceedings(self):
        bib = generate_bibtex(self._make_work(type="proceedings-article"))
        assert bib.startswith("@inproceedings{")
        assert "booktitle={Test Journal}" in bib
        assert "journal=" not in bib

    def test_book_chapter(self):
        bib = generate_bibtex(self._make_work(type="book-chapter"))
        assert bib.startswith("@incollection{")
        assert "booktitle={Test Journal}" in bib

    def test_dissertation(self):
        work = self._make_work(
            type="dissertation",
            authorships=[
                {
                    "author": {"display_name": "Jane Doe"},
                    "institutions": [{"display_name": "MIT"}],
                }
            ],
        )
        bib = generate_bibtex(work)
        assert bib.startswith("@phdthesis{")
        assert "school={MIT}" in bib

    def test_unknown_type_falls_back_to_misc(self):
        bib = generate_bibtex(self._make_work(type="something-new"))
        assert bib.startswith("@misc{")

    def test_special_chars_escaped(self):
        bib = generate_bibtex(self._make_work(title="ML & Drug Discovery: 100% Effective"))
        assert r"ML \& Drug Discovery: 100\% Effective" in bib

    def test_no_pages_when_missing(self):
        bib = generate_bibtex(
            self._make_work(biblio={"volume": None, "issue": None, "first_page": None, "last_page": None})
        )
        assert "pages=" not in bib
        assert "volume=" not in bib

    def test_arxiv_preprint_has_eprint(self):
        bib = generate_bibtex(
            self._make_work(
                type="preprint",
                doi="https://doi.org/10.48550/arXiv.1706.03762",
            )
        )
        assert "eprint={1706.03762}" in bib
        assert "archiveprefix={arXiv}" in bib

    def test_techreport(self):
        bib = generate_bibtex(self._make_work(type="report"))
        assert bib.startswith("@techreport{")
        assert "institution={Test Journal}" in bib
        assert "journal=" not in bib

    def test_underscore_and_hash_escaped(self):
        bib = generate_bibtex(self._make_work(title="A_B #1 Study"))
        assert r"A\_B \#1 Study" in bib

from academic_tools_mcp.bibtex import (
    _extract_last_name,
    _format_authors_bibtex,
    _generate_key,
    generate_arxiv_bibtex,
    generate_bibtex,
    generate_biorxiv_bibtex,
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


class TestGenerateArxivBibtex:
    def _make_arxiv_paper(self, **overrides):
        base = {
            "id": "http://arxiv.org/abs/1706.03762v7",
            "title": "Attention Is All You Need",
            "summary": "The dominant sequence transduction models...",
            "published": "2017-06-12T17:57:34Z",
            "updated": "2023-08-02T00:52:10Z",
            "authors": [
                {"name": "Ashish Vaswani", "affiliations": ["Google Brain"]},
                {"name": "Noam Shazeer", "affiliations": []},
            ],
            "categories": ["cs.CL", "cs.LG"],
            "primary_category": "cs.CL",
            "links": [
                {"href": "http://arxiv.org/abs/1706.03762v7", "rel": "alternate", "title": None},
                {"href": "http://arxiv.org/pdf/1706.03762v7", "rel": "related", "title": "pdf"},
            ],
            "comment": "15 pages, 5 figures",
            "journal_ref": None,
            "doi": None,
        }
        base.update(overrides)
        return base

    def test_preprint_is_misc(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert bib.startswith("@misc{")

    def test_published_is_article(self):
        bib = generate_arxiv_bibtex(
            self._make_arxiv_paper(journal_ref="Advances in Neural Information Processing Systems 30 (2017)")
        )
        assert bib.startswith("@article{")
        assert "journal={Advances in Neural Information Processing Systems 30 (2017)}" in bib

    def test_has_eprint_field(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert "eprint={1706.03762}" in bib

    def test_has_archiveprefix(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert "archiveprefix={arXiv}" in bib

    def test_has_primaryclass(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert "primaryclass={cs.CL}" in bib

    def test_key_generation(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert bib.startswith("@misc{vaswani2017attention,")

    def test_doi_included_when_present(self):
        bib = generate_arxiv_bibtex(
            self._make_arxiv_paper(doi="10.48550/arXiv.1706.03762")
        )
        assert "doi={10.48550/arXiv.1706.03762}" in bib

    def test_no_doi_when_absent(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        assert "doi=" not in bib

    def test_special_chars_escaped(self):
        bib = generate_arxiv_bibtex(
            self._make_arxiv_paper(title="ML & Drug Discovery: 100% Effective")
        )
        assert r"ML \& Drug Discovery: 100\% Effective" in bib

    def test_particle_author(self):
        paper = self._make_arxiv_paper(
            authors=[
                {"name": "Ludwig van den Berg", "affiliations": []},
            ]
        )
        bib = generate_arxiv_bibtex(paper)
        assert "author={van den Berg, Ludwig}" in bib

    def test_no_authors(self):
        paper = self._make_arxiv_paper(authors=[])
        bib = generate_arxiv_bibtex(paper)
        assert "unknown2017" in bib
        assert "author=" not in bib

    def test_eprint_strips_version(self):
        bib = generate_arxiv_bibtex(self._make_arxiv_paper())
        # ID is 1706.03762v7, eprint should be 1706.03762
        assert "eprint={1706.03762}" in bib
        assert "v7" not in bib.split("eprint=")[1].split(",")[0]


# ---------------------------------------------------------------------------
# bioRxiv BibTeX
# ---------------------------------------------------------------------------


class TestGenerateBiorxivBibtex:
    @staticmethod
    def _make_biorxiv_paper(**overrides):
        base = {
            "doi": "10.1101/2024.01.01.573838",
            "title": "A Great Discovery in Cell Biology",
            "authors": [
                {"name": "S. Fujii"},
                {"name": "Y. Wang"},
            ],
            "date": "2024-01-02",
            "version": "2",
            "server": "biorxiv",
            "published_doi": None,
            "category": "cell biology",
        }
        base.update(overrides)
        return base

    def test_preprint_is_misc(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert bib.startswith("@misc{")

    def test_published_is_article(self):
        bib = generate_biorxiv_bibtex(
            self._make_biorxiv_paper(published_doi="10.1038/s41586-024-00001-1")
        )
        assert bib.startswith("@article{")

    def test_published_uses_journal_doi(self):
        bib = generate_biorxiv_bibtex(
            self._make_biorxiv_paper(published_doi="10.1038/s41586-024-00001-1")
        )
        assert "doi={10.1038/s41586-024-00001-1}" in bib

    def test_preprint_has_publisher(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert "publisher={bioRxiv}" in bib

    def test_medrxiv_publisher(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper(server="medrxiv"))
        assert "publisher={medRxiv}" in bib

    def test_preprint_has_doi(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert "doi={10.1101/2024.01.01.573838}" in bib

    def test_preprint_has_howpublished(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert r"\url{https://doi.org/10.1101/2024.01.01.573838}" in bib

    def test_key_generation(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert bib.startswith("@misc{fujii2024great,")

    def test_year_from_date(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper())
        assert "year={2024}" in bib

    def test_special_chars_escaped(self):
        bib = generate_biorxiv_bibtex(
            self._make_biorxiv_paper(title="Drug & Target: 100% Binding")
        )
        assert r"Drug \& Target: 100\% Binding" in bib

    def test_no_authors(self):
        bib = generate_biorxiv_bibtex(self._make_biorxiv_paper(authors=[]))
        assert "unknown2024" in bib
        assert "author=" not in bib

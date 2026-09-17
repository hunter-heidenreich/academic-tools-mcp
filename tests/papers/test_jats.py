"""bioRxiv's JATS XML to markdown: ``papers.jats``.

The fixture is trimmed from a real bioRxiv source.xml (10.1101/2020.03.09.983247) —
HighWire's namespace declarations, ``hwp:`` attributes, ``object-id`` noise and
``citation`` markup kept as served. Math, tables and lists are synthetic: that paper
ships its table as an image and has no formulas.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from academic_tools_mcp.papers import jats, sections

_ROOT_OPEN = (
    '<article article-type="article" specific-use="production" xml:lang="en" '
    'xmlns:hw="org.highwire.hpp" xmlns:mml="http://www.w3.org/1998/Math/MathML" '
    'xmlns:hwp="http://schema.highwire.org/Journal" xmlns:ref="http://schema.highwire.org/Reference" '
    'xmlns:xlink="http://www.w3.org/1999/xlink">'
)


def _article(front: str = "", body: str = "", back: str = "") -> str:
    return f"{_ROOT_OPEN}<front>{front}</front><body>{body}</body><back>{back}</back></article>"


def _meta(inner: str) -> str:
    return (
        '<journal-meta><journal-id journal-id-type="hwp">biorxiv</journal-id></journal-meta>'
        f"<article-meta>{inner}</article-meta>"
    )


_REAL = _article(
    front=_meta(
        '<article-id pub-id-type="doi">10.1101/2020.03.09.983247</article-id>'
        '<title-group><article-title hwp:id="article-title-1">Inhibition of SARS-CoV-2 infection'
        "</article-title></title-group>"
        '<abstract hwp:id="abstract-1"><title hwp:id="title-1">Abstract</title>'
        '<p hwp:id="p-2">The recent outbreak of coronavirus disease (COVID-19) with IC'
        "<sub>50</sub>s of 1.3 nM.</p></abstract>"
    ),
    body=(
        '<sec id="s1" hwp:id="sec-1"><title hwp:id="title-3">Introduction</title>'
        '<p hwp:id="p-4">Disease X, caused by an unknown pathogen<sup>'
        '<xref ref-type="bibr" rid="c1" hwp:id="xref-ref-1-1">1</xref>,'
        '<xref ref-type="bibr" rid="c2">2</xref></sup> '
        '(<xref ref-type="fig" rid="fig1">Fig.1a</xref>).</p>'
        '<fig id="fig1" position="float" fig-type="figure" hwp:id="F1">'
        '<object-id pub-id-type="other" hwp:sub-type="pisa">biorxiv;2020.03.09.983247v1/FIG1</object-id>'
        "<label>Fig. 1.</label>"
        '<caption hwp:id="caption-1"><title hwp:id="title-4">Establishment of the fusion system</title>'
        "<p><bold>a</bold>. The emerging timeline.</p></caption>"
        '<graphic xlink:href="983247v1_fig1" position="float"/></fig>'
        "</sec>"
        '<sec id="s2" hwp:id="sec-2"><title>Results</title>'
        '<sec id="s2a"><title>The capacity of membrane fusion</title><p>Fusion was superior.</p>'
        '<table-wrap id="T1" hwp:id="T1"><object-id pub-id-type="other">TBL1</object-id>'
        "<label>Table 1.</label><caption><title>Data collection statistics</title></caption>"
        '<graphic xlink:href="983247v1_tbl1"/></table-wrap>'
        "</sec></sec>"
    ),
    back=(
        '<sec sec-type="COI-statement" hwp:id="sec-26"><title>Conflicts of interest</title>'
        "<p>The authors declare no conflict of interest.</p></sec>"
        '<ref-list hwp:id="ref-list-1"><title hwp:id="title-35">References</title>'
        '<ref id="c1" hwp:id="ref-1"><label>1.</label>'
        '<citation publication-type="website" ref:id="2020.03.09.983247v1.1" hwp:id="citation-1">'
        '<collab hwp:id="collab-1">WHO</collab>. <article-title>Blueprint for R&amp;D preparedness'
        '</article-title>. <ext-link ext-link-type="uri" xlink:href="http://www.who.int/x">'
        "http://www.who.int/x</ext-link> (<year>2015</year>).</citation></ref>"
        '<ref id="c32"><label>32.</label><citation publication-type="journal">'
        '<string-name name-style="western"><surname>Xia</surname>, <given-names>S.</given-names>'
        "</string-name>, <etal>et al.</etal> <article-title>Potent MERS-CoV fusion inhibitory "
        "peptides</article-title>. <source>Viruses</source> <volume>11</volume>(<year>2019</year>)."
        "</citation></ref></ref-list>"
    ),
)


class TestDocumentStructure:
    def test_the_real_article_renders_in_order(self):
        assert jats.to_markdown(_REAL) == (
            "# Inhibition of SARS-CoV-2 infection\n\n"
            "## Abstract\n\n"
            "The recent outbreak of coronavirus disease (COVID-19) with IC50s of 1.3 nM.\n\n"
            "## Introduction\n\n"
            "Disease X, caused by an unknown pathogen1,2 (Fig.1a).\n\n"
            "Fig. 1. Establishment of the fusion system a. The emerging timeline.\n\n"
            "## Results\n\n"
            "### The capacity of membrane fusion\n\n"
            "Fusion was superior.\n\n"
            "Table 1. Data collection statistics\n\n"
            "## Conflicts of interest\n\n"
            "The authors declare no conflict of interest.\n\n"
            "## References\n\n"
            "- 1. WHO. Blueprint for R&D preparedness. http://www.who.int/x (2015).\n\n"
            "- 32. Xia, S., et al. Potent MERS-CoV fusion inhibitory peptides. Viruses 11(2019).\n"
        )

    def test_sections_are_detected_from_the_headings(self):
        titles, detected = sections.parse_sections_and_detect(jats.to_markdown(_REAL))
        assert detected is True
        assert [s["title"] for s in titles] == [
            "Abstract",
            "Introduction",
            "Results",
            "Conflicts of interest",
            "References",
        ]
        assert titles[2]["h3s"] == ["The capacity of membrane fusion"]

    def test_depth_is_capped_at_four(self):
        nested = "<sec><title>A</title>" * 6 + "<p>deep</p>" + "</sec>" * 6
        md = jats.to_markdown(_article(body=nested))
        assert [line for line in md.splitlines() if line.startswith("#")] == [
            "## A",
            "### A",
            "#### A",
            "#### A",
            "#### A",
            "#### A",
        ]

    def test_a_default_namespace_is_ignored(self):
        xml = (
            '<article xmlns="https://jats.nlm.nih.gov/ns/archiving/1.3/"><body>'
            "<sec><title>Intro</title><p>Text.</p></sec></body></article>"
        )
        assert jats.to_markdown(xml) == "## Intro\n\nText.\n"

    def test_graphical_abstracts_are_skipped(self):
        front = _meta(
            "<abstract><p>Real.</p></abstract>"
            '<abstract abstract-type="graphical"><p>Picture.</p></abstract>'
        )
        md = jats.to_markdown(_article(front=front))
        assert md == "## Abstract\n\nReal.\n"

    def test_an_untitled_acknowledgment_gets_a_heading(self):
        md = jats.to_markdown(_article(back="<ack><p>Thanks.</p></ack>"))
        assert md == "## Acknowledgments\n\nThanks.\n"

    def test_an_untitled_section_opens_no_heading(self):
        md = jats.to_markdown(_article(body="<sec><p>Orphan.</p></sec>"))
        assert md == "Orphan.\n"

    def test_body_text_opening_with_a_hash_is_escaped(self):
        md = jats.to_markdown(_article(body="<sec><title>S</title><p># not a heading</p></sec>"))
        assert "\\# not a heading" in md


class TestMathTablesLists:
    def test_tex_math_is_unwrapped_from_its_latex_document(self):
        formula = (
            '<disp-formula id="e1"><alternatives><tex-math>\\documentclass{article}'
            "\\begin{document}$$E = mc^2$$\\end{document}</tex-math>"
            '<graphic xlink:href="eq1"/></alternatives></disp-formula>'
        )
        md = jats.to_markdown(_article(body=f"<sec><title>S</title>{formula}</sec>"))
        assert "$$E = mc^2$$" in md

    def test_mathml_falls_back_to_alttext(self):
        para = (
            '<p>Rate <inline-formula><mml:math alttext="k_{on}"><mml:mi>k</mml:mi></mml:math>'
            "</inline-formula> rose.</p>"
        )
        assert jats.to_markdown(_article(body=para)) == "Rate $k_{on}$ rose.\n"

    def test_a_formula_with_only_a_graphic_renders_nothing(self):
        para = '<p>See <inline-formula><inline-graphic xlink:href="i1"/></inline-formula>.</p>'
        assert jats.to_markdown(_article(body=para)) == "See .\n"

    def test_a_table_is_a_pipe_table_after_its_caption(self):
        table = (
            "<table-wrap><label>Table 2.</label><caption><p>IC50 by virus</p></caption>"
            "<table><thead><tr><th>Virus</th><th>IC50 (nM)</th></tr></thead>"
            '<tbody><tr><td rowspan="2">SARS-CoV-2</td><td>1.3</td></tr>'
            "<tr><td>15.8</td></tr></tbody></table></table-wrap>"
        )
        assert jats.to_markdown(_article(body=table)) == (
            "Table 2. IC50 by virus\n\n"
            "| Virus | IC50 (nM) |\n"
            "| --- | --- |\n"
            "| SARS-CoV-2 | 1.3 |\n"
            "|  | 15.8 |\n"
        )

    def test_a_list_is_bulleted(self):
        items = '<list list-type="bullet"><list-item><p>One</p></list-item><list-item><p>Two</p></list-item></list>'
        assert jats.to_markdown(_article(body=items)) == "- One\n\n- Two\n"

    def test_supplementary_material_is_dropped(self):
        supp = (
            '<supplementary-material><label>Data S1</label><media xlink:href="s1.xlsx"/>'
            "</supplementary-material><p>Kept.</p>"
        )
        assert jats.to_markdown(_article(body=supp)) == "Kept.\n"


class TestDegenerateInput:
    @pytest.mark.parametrize("xml", ["", "   ", "<article>", "not xml at all", "<a></b>"])
    def test_unparseable_input_is_empty(self, xml):
        assert jats.to_markdown(xml) == ""

    def test_an_entity_declaration_is_refused_not_expanded(self):
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE article [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            "<article><body><p>&b;</p></body></article>"
        )
        assert jats.to_markdown(bomb) == ""

    def test_an_article_with_no_text_is_empty(self):
        assert jats.to_markdown(_article()) == ""


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_arbitrary_text_never_raises(text):
    jats.to_markdown(text)


_TAGS = [
    "article",
    "front",
    "article-meta",
    "article-title",
    "abstract",
    "body",
    "back",
    "sec",
    "title",
    "p",
    "fig",
    "label",
    "caption",
    "table-wrap",
    "table",
    "tr",
    "td",
    "list",
    "list-item",
    "ref-list",
    "ref",
    "disp-formula",
    "inline-formula",
    "tex-math",
    "ack",
]


@st.composite
def _trees(draw, depth=0):
    tag = draw(st.sampled_from(_TAGS))
    if depth >= 4:
        return f"<{tag}>{draw(st.text(alphabet='ab #$|', max_size=5))}</{tag}>"
    children = draw(st.lists(_trees(depth=depth + 1), max_size=3))
    return f"<{tag}>{draw(st.text(alphabet='ab #$|', max_size=3))}{''.join(children)}</{tag}>"


@settings(max_examples=200, deadline=None)
@given(_trees())
def test_arbitrary_jats_shaped_trees_never_raise(xml):
    jats.to_markdown(xml)

"""Markdown documents the section suites parse.

One copy, because the section reader, the converter and the corpus search all
have to agree about what these documents contain.
"""

# ---------------------------------------------------------------------------
# Fixtures: H2-based document (standard markdown)
# ---------------------------------------------------------------------------

_H2_MARKDOWN = """\
Some preamble text before any heading.

This has multiple lines.

## Introduction

This is the introduction section.

It has multiple paragraphs.

### Background

Some background information here.

### Motivation

Why we did this work.

## Related Work

Previous approaches to the problem.

## Methods

### Architecture

The model architecture is described here.

### Training

Training details go here with specifics.

More training details on a second paragraph.

## Results

We achieved state-of-the-art performance.

## Conclusion

In this paper, we presented our approach.
"""

# ---------------------------------------------------------------------------
# Fixtures: H1-based document (MinerU output style)
# ---------------------------------------------------------------------------

_H1_MARKDOWN = """\
# Attention Is All You Need

Ashish Vaswani, Noam Shazeer

# Abstract

The dominant sequence transduction models are based on complex recurrent neural networks.

# 1 Introduction

Recurrent neural networks have been firmly established as state of the art.

Attention mechanisms have become an integral part of sequence modeling.

# 2 Background

The goal of reducing sequential computation forms the foundation of several approaches.

# 3 Model Architecture

Most competitive neural sequence transduction models have an encoder-decoder structure.

# 3.1 Encoder and Decoder Stacks

The encoder is composed of a stack of N=6 identical layers.

# 3.2 Attention

An attention function maps a query and key-value pairs to an output.

# 4 Why Self-Attention

In this section we compare various aspects of self-attention layers.

# 5 Training

We describe the training regime for our models.

# 5.1 Training Data and Batching

We trained on the WMT 2014 English-German dataset.

# 6 Results

Results on machine translation and other tasks.

# 7 Conclusion

In this work, we presented the Transformer.
"""

_NO_HEADINGS = """\
Just a document with no headings at all.

It has some content but no structure.
"""

_H2_ONLY_MARKDOWN = """\
## First Section

Content of first section.

## Second Section

Content of second section.
"""


# ---------------------------------------------------------------------------
# parse_sections regression: more H3 subsections than H2 sections
# ---------------------------------------------------------------------------


_H2_WITH_MANY_H3S = """\
## Title
Preamble-ish.

## Results
### Sub A
text
### Sub B
text
### Sub C
text
### Sub D
text

## Methods
### Method A
text
### Method B
text
### Method C
text

## References
### Refs 1-10
text
### Refs 11-20
text
### Refs 21-30
text
"""

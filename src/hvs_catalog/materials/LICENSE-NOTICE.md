# License and Provenance of Imported Code

Some foundational algorithms in `hvs_catalog.materials` and
`hvs_catalog.records.tabular` were adapted from `stella_hvs` at reference commit
`8421c98`. The full applicable copyright and MIT license below accompany this
repository and its wheel/sdist distributions. They cover the imported and adapted
content only, without assigning a license to unrelated new code or expanding
publication authorization.

Imported modules, also identified in their module docstrings:

- `materials/cleaning.py`: TeX comment cleanup preserving lines and verbatim text,
  from `src/stella/lit/extraction/cleaning.py`.
- `materials/texgraph.py`: main-document and include-graph algorithms, from
  `src/stella/lit/extraction/tex_graph.py`.
- `materials/ranges.py`: deterministic identifier-range expansion, from
  `src/stella/lit/extraction/range_expand.py`.
- `materials/convert.py`: basic LaTeX row/cell parsing, HTML table parsing, and
  pgfplotstable association tools, from `src/stella/lit/catalog_extraction.py`.
- `records/tabular.py`: foundational ECSV header tools based on
  `src/stella/lit/extraction/ecsv.py`. Current entry points strictly validate
  metadata rather than certifying unknown fields or unsupported types through
  tolerant fallbacks.

The legacy repository is not distributed here or with the wheel. The new program
has no dependency on it or machine-specific paths. Legacy directory structures,
global state, task engines, and real data were not imported.

---

MIT License

Copyright (c) 2026 Zhang Jian

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

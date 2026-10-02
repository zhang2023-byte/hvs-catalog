# 迁入代码的许可与来源说明

本包（`hvs_catalog.materials` 及 `hvs_catalog.records.tabular`）中部分基
础算法迁自旧仓 `stella_hvs`（参考提交 `8421c98`）。按 MIT 许可的要求，
迁入内容适用的完整版权与许可声明随本仓源码及发行包（wheel／sdist）
一并携带如下；它只覆盖迁入改造的内容，不为无关新代码另定许可或扩大
发布授权。

迁入并按本仓设计改造的模块（模块 docstring 另有标注）：

- `materials/cleaning.py`：TeX 注释清理（保持行位置、保护 verbatim），
  迁自 `src/stella/lit/extraction/cleaning.py`；
- `materials/texgraph.py`：主稿与包含关系解析，基础算法迁自
  `src/stella/lit/extraction/tex_graph.py`；
- `materials/ranges.py`：编号范围确定性展开，迁自
  `src/stella/lit/extraction/range_expand.py`；
- `materials/convert.py`：LaTeX 行／单元解析、HTML 表解析与 pgfplotstable
  关联的基础工具，迁自 `src/stella/lit/catalog_extraction.py`；
- `records/tabular.py`：ECSV 表头读取的基础工具，参考
  `src/stella/lit/extraction/ecsv.py`；当前入口按规范严格核验元数据，
  不再用宽容回退认证未知字段或不支持类型。

旧仓不随本仓或 wheel 分发；新程序不依赖旧仓或本机专用路径。未迁入旧
仓的目录结构、全局状态、任务引擎或真实数据。

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

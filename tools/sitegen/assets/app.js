"use strict";
const main = document.querySelector("main");
const dialog = document.querySelector("dialog");
const body = document.querySelector("#dialog-body");
let index;
let navigation = 0;
const state = { search: "", velocity: false, multiple: false, sort: "galactic_rest_frame_velocity", ascending: false };
const cache = new Map();
document.querySelector(".skip").onclick = event => { event.preventDefault(); main.focus(); };
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function link(text, url) {
  const node = el("a", text); node.href = url;
  if (url.startsWith("https://")) { node.target = "_blank"; node.rel = "noopener noreferrer"; }
  return node;
}
function button(text, action, className) {
  const node = el("button", text, className); node.type = "button";
  node.addEventListener("click", action); return node;
}
function openDialog(title) {
  document.querySelector("#dialog-title").textContent = title;
  body.replaceChildren();
  if (!dialog.open) dialog.showModal();
}
document.querySelector("#close-dialog").onclick = () => dialog.close();
document.querySelector("#downloads").onclick = () => {
  openDialog("Download catalog");
  body.append(el("p", `Data version ${index.data_version}`, "muted"));
  for (const [name, url] of [["Complete catalog ZIP", "downloads/catalog.zip"], ["stars.csv", "downloads/stars.csv"]]) {
    const p = el("p"); const a = link(name, url); a.download = ""; p.append(a); body.append(p);
  }
};
function evidence(value) {
  openDialog("Measurement evidence");
  body.append(link(value.source.label, value.source.url), el("p", value.source.title, "muted"));
  body.append(el("p", value.display + (value.unit ? ` ${value.unit}` : ""), "num"));
  if (value.reported) body.append(el("p", value.reported));
  if (value.label) body.append(el("p", value.label, "quoted"));
  if (value.condition) body.append(el("p", value.condition));
  if (value.correction_notice) body.append(el("p", value.correction_notice));
  for (const item of value.evidence) {
    const section = el("div", undefined, "evidence-page");
    section.append(link(`Fixed PDF · page ${item.page}`, item.url));
    for (const quote of item.quotes) {
      if (quote.label) section.append(el("p", quote.label, "muted"));
      section.append(el("blockquote", quote.text));
    }
    body.append(section);
  }
}
function valueButton(value, withUnit = false) {
  const text = withUnit ? value.display + (value.unit ? ` ${value.unit}` : "") : value.table_display;
  const node = button(text, () => evidence(value), "value num");
  node.title = value.tooltip;
  node.setAttribute("aria-label", `${node.textContent}; ${value.tooltip}; open evidence`);
  return node;
}
function valueList(title, values) {
  openDialog(title);
  for (const value of values) {
    const item = el("div", undefined, "value-detail" + (value.label ? " quoted" : ""));
    item.append(valueButton(value, true));
    if (value.label) item.append(el("p", value.label));
    if (value.condition) item.append(el("p", value.condition));
    body.append(item);
  }
}
function tableWrapper(table) {
  const wrapper = el("div", undefined, "table-scroll"); wrapper.tabIndex = 0;
  wrapper.setAttribute("role", "region"); wrapper.setAttribute("aria-label", table.getAttribute("aria-label") + "; scroll horizontally for more columns");
  wrapper.append(table); return wrapper;
}
function checkbox(text, checked, action) {
  const label = el("label"); const input = el("input"); input.type = "checkbox"; input.checked = checked;
  input.addEventListener("change", () => action(input.checked)); label.append(input, document.createTextNode(text)); return label;
}
function home() {
  document.title = "HVS Catalog";
  const intro = el("div", undefined, "intro");
  intro.append(el("p", `Published measurements of hypervelocity star candidates from ${index.contributing_papers} papers.`), el("p", "Values were extracted automatically; check the original paper before use.", "notice"));
  main.replaceChildren(intro);
  if (index.preview.length) {
    const preview = el("div", undefined, "preview");
    preview.append(el("p", "Preview · Selected stellar pages are available for review. The table and downloads include the full catalog."));
    for (const row of index.preview) preview.append(link(row.name, `#/star/${row.slug}`));
    main.append(preview);
  }
  const controls = el("div", undefined, "controls");
  const search = el("input"); search.type = "search"; search.placeholder = "Search name, alias or Gaia DR3 ID";
  search.setAttribute("aria-label", search.placeholder); search.value = state.search;
  search.addEventListener("input", () => { state.search = search.value; renderRows(); });
  const count = el("span"); count.id = "count"; count.setAttribute("aria-live", "polite");
  controls.append(search, checkbox("Has v_GRF", state.velocity, x => { state.velocity = x; renderRows(); }), checkbox("Studied in ≥2 papers", state.multiple, x => { state.multiple = x; renderRows(); }), count);
  main.append(controls);
  const table = el("table", undefined, "catalog"); table.setAttribute("aria-label", "Stellar catalog");
  const head = el("thead"); const hr = el("tr"); const rows = el("tbody");
  const columns = [{ key: "catalog_number", label: "ID" }, { key: "name", label: "Name" }, ...index.columns, { key: "papers", label: "Papers" }, { key: "first", label: "First" }];
  const headings = [];
  for (const column of columns) {
    const th = el("th"); th.scope = "col";
    if (column.key === "catalog_number") th.className = "catalog-number";
    if (column.key === "name") th.className = "catalog-name";
    const b = button(column.label, () => {
      state.ascending = state.sort === column.key ? !state.ascending : ["name", "catalog_number"].includes(column.key);
      state.sort = column.key; renderRows();
    });
    th.append(b); if (column.unit) th.append(el("span", `[${column.unit}]`, "unit"));
    headings.push([th, b, column]); hr.append(th);
  }
  head.append(hr); table.append(head, rows); main.append(tableWrapper(table));
  function renderRows() {
    const query = state.search.toLowerCase().replace(/\s/g, "");
    const shown = index.rows.filter(row => row.search.includes(query) && (!state.velocity || row.values.galactic_rest_frame_velocity) && (!state.multiple || row.papers >= 2));
    shown.sort((a, b) => {
      let av = a[state.sort], bv = b[state.sort];
      if (state.sort in a.values) {
        const aa = a.values[state.sort], bb = b.values[state.sort];
        const group = (aa?.sort_group ?? 2) - (bb?.sort_group ?? 2);
        if (group) return group;
        av = aa?.sort_value ?? 0; bv = bb?.sort_value ?? 0;
      }
      const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return (state.ascending ? cmp : -cmp) || a.name.localeCompare(b.name);
    });
    for (const [th, b, column] of headings) {
      th.setAttribute("aria-sort", state.sort === column.key ? (state.ascending ? "ascending" : "descending") : "none");
      b.textContent = column.label + (state.sort === column.key ? (state.ascending ? " ↑" : " ↓") : "");
    }
    count.textContent = `${shown.length} stars`;
    rows.replaceChildren();
    for (const row of shown) {
      const tr = el("tr"); const name = el("td", undefined, "catalog-name");
      tr.append(el("td", row.catalog_number, "catalog-number"));
      name.append(row.available ? link(row.name, `#/star/${row.slug}`) : document.createTextNode(row.name)); tr.append(name);
      for (const column of index.columns) {
        const value = row.values[column.key]; const td = el("td", value?.display ?? "—");
        if (value) td.title = value.tooltip; tr.append(td);
      }
      tr.append(el("td", row.papers), el("td", row.first)); rows.append(tr);
    }
    if (!shown.length) { const tr = el("tr"); const td = el("td", "No stars match your search and filters."); td.colSpan = columns.length; tr.append(td); rows.append(tr); }
  }
  renderRows();
}
function star(view) {
  document.title = `${view.name} · HVS Catalog`;
  main.replaceChildren(link("All stars", "#/"), el("p", view.catalog_id, "catalog-label num"), el("h1", view.name));
  if (view.aliases.length) {
    const aliases = el("p", `also reported as ${view.aliases.join(" · ")}`, "identity aliases");
    if (view.more_aliases.length) {
      const extra = el("span", " · " + view.more_aliases.join(" · "));
      extra.id = "additional-aliases"; extra.hidden = true;
      const more = button(view.alias_more_label, () => {
        extra.hidden = !extra.hidden;
        more.textContent = extra.hidden ? view.alias_more_label : "Show less";
        more.setAttribute("aria-expanded", String(!extra.hidden));
      });
      more.setAttribute("aria-expanded", "false"); more.setAttribute("aria-controls", extra.id);
      aliases.append(extra, more);
    }
    main.append(aliases);
  }
  if (view.gaia_line) main.append(el("p", view.gaia_line, "identity num gaia"));
  if (view.coordinates) main.append(el("p", view.coordinates.text, "identity num"));
  const links = el("div", undefined, "links");
  if (view.simbad_url) { const span = el("span"); span.append(link("SIMBAD", view.simbad_url)); if (view.simbad_position) span.append(document.createTextNode(" · matched by position")); links.append(span); }
  if (view.gaia_url) links.append(link("Gaia DR3", view.gaia_url));
  main.append(links);
  if (view.position_note) main.append(el("p", "Some papers were linked by position only.", "notice"));
  const latest = el("section"); latest.append(el("h2", "Latest values"));
  const latestTable = el("table"); latestTable.setAttribute("aria-label", "Latest measurements"); const latestBody = el("tbody");
  for (const column of view.columns) {
    const value = view.latest[column.key]; if (!value) continue;
    const row = el("tr"); const cell = el("td"); cell.append(valueButton(value, true));
    const source = el("td"); source.append(link(value.source.label, value.source.url)); row.append(el("td", column.label), cell, source); latestBody.append(row);
  }
  if (latestBody.children.length) {
    latestTable.append(latestBody); const wrap = tableWrapper(latestTable); wrap.classList.add("latest"); latest.append(wrap);
  } else latest.append(el("p", "No default values are available.", "muted"));
  main.append(latest);
  const literature = el("section"); const heading = el("div", undefined, "section-heading");
  let showExtra = view.literature_defaults.show_quoted, showEmpty = view.literature_defaults.show_empty;
  const filters = el("div", undefined, "literature-filters");
  filters.append(
    checkbox("Show quoted and non-comparable values", showExtra, checked => { showExtra = checked; renderLiterature(); }),
    checkbox("Show papers without visible values", showEmpty, checked => { showEmpty = checked; renderLiterature(); })
  );
  heading.append(el("h2", "Literature"), filters);
  const table = el("table", undefined, "literature"); table.setAttribute("aria-label", "Literature measurements");
  const head = el("thead"); const row = el("tr");
  for (const column of view.literature_identity_columns) row.append(el("th", column.label, `literature-${column.key}`));
  for (const column of view.columns) { const th = el("th", column.label); if (column.unit) th.append(el("span", `[${column.unit}]`, "unit")); row.append(th); }
  head.append(row); const rows = el("tbody"); table.append(head, rows); literature.append(heading, tableWrapper(table)); main.append(literature);
  function renderLiterature() {
    rows.replaceChildren();
    for (const paper of view.literature) {
      if (!showEmpty && !(showExtra ? paper.has_values : paper.has_own_values)) continue;
      const row = el("tr"); const source = el("td"); const a = link(paper.source.short, paper.source.url); a.title = paper.source.title; source.append(a);
      for (const column of view.literature_identity_columns) {
        const cell = column.key === "paper" ? source : el("td", paper.source.year);
        cell.className = `literature-${column.key}`; row.append(cell);
      }
      for (const column of view.columns) {
        const cell = paper.cells[column.key]; const td = el("td");
        td.append(cell.main ? valueButton(cell.main) : document.createTextNode("—"));
        if (cell.others.length) td.append(button(cell.more_label, () => valueList(`${paper.source.label} · ${column.label}`, [cell.main, ...cell.others]), "more"));
        if (showExtra && cell.extra.length) {
          for (const value of cell.extra) {
            const extra = el("div", undefined, "quoted"); extra.append(valueButton(value), el("span", ` · ${value.label}`)); td.append(extra);
          }
        }
        row.append(td);
      }
      rows.append(row);
    }
  }
  renderLiterature();
  const footer = el("div", undefined, "page-footer"); const download = link("Download JSON", view.download); download.download = "";
  footer.append(download, link("Report an error", view.issue_url)); main.append(footer);
}
function about(view) {
  document.title = `${view.title} · HVS Catalog`;
  const article = el("article", undefined, "about");
  article.append(el("h1", view.title));
  for (const item of view.sections) {
    const section = el("section"); section.append(el("h2", item.title));
    for (const paragraph of item.paragraphs) section.append(el("p", paragraph));
    if (item.code) { const pre = el("pre"); pre.append(el("code", item.code)); section.append(pre); }
    if (item.links) {
      const links = el("div", undefined, "links");
      for (const itemLink of item.links) {
        const a = link(itemLink.label, itemLink.url); if (itemLink.download) a.download = "";
        links.append(a);
      }
      section.append(links);
    }
    article.append(section);
  }
  main.replaceChildren(link("All stars", "#/"), article);
}
async function route() {
  const token = ++navigation;
  if (dialog.open) dialog.close();
  try {
    const hash = location.hash || "#/";
    if (hash === "#/" || hash === "#main") home();
    else if (hash === "#/about") {
      main.replaceChildren(el("p", "Loading about…"));
      if (!cache.has("#/about")) { const response = await fetch("about.json"); if (!response.ok) throw new Error("Could not load About."); cache.set("#/about", await response.json()); }
      if (token !== navigation) return;
      about(cache.get("#/about"));
    } else if (hash.startsWith("#/star/")) {
      const slug = hash.slice(7); const row = index.rows.find(row => row.slug === slug && row.available);
      if (!row) throw new Error("This stellar page is not available.");
      main.replaceChildren(el("p", "Loading star…"));
      if (!cache.has(slug)) { const response = await fetch(`views/${encodeURIComponent(slug)}.json`); if (!response.ok) throw new Error("Could not load stellar page."); cache.set(slug, await response.json()); }
      if (token !== navigation) return;
      star(cache.get(slug));
    } else throw new Error("This page is not available.");
    window.scrollTo(0, 0);
  } catch (error) { main.replaceChildren(el("h1", "Page unavailable"), el("p", error.message), link("All stars", "#/")); }
}
(async () => {
  try {
    const response = await fetch("index.json"); if (!response.ok) throw new Error("Could not load catalog."); index = await response.json();
    document.querySelector("#version").textContent = `Data version ${index.data_version}`;
    window.addEventListener("hashchange", route); await route();
  } catch (error) { main.replaceChildren(el("p", error.message)); }
})();

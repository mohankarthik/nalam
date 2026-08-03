/* Client-side list filter. One bar per page (`.filter-bar`); the rows or
   cards it filters carry `data-filter` plus any of:
     data-filter-cat   -- `|`-joined categories (segment, study type, status...)
     data-filter-kind  -- single value for the independent Kind dropdown
     data-filter-date   -- ISO date; compared as a string against From/To
     data-filter-text   -- free-text haystack, matched case-insensitively
   A `[data-filter-group]` container (an observations segment panel) hides
   itself when none of its items survive. Everything here only hides DOM the
   server already scoped to one person -- it changes nothing about the data. */
(function () {
  const bar = document.querySelector(".filter-bar");
  if (!bar) return;
  const scope = document.querySelector("main");
  const items = Array.from(scope.querySelectorAll("[data-filter]"));
  const groups = Array.from(scope.querySelectorAll("[data-filter-group]"));
  const empties = Array.from(scope.querySelectorAll(".js-filter-empty"));

  const catSel = bar.querySelector(".filter-cat");
  const kindSel = bar.querySelector(".filter-kind");
  const fromInp = bar.querySelector(".filter-from");
  const toInp = bar.querySelector(".filter-to");
  const textInp = bar.querySelector(".filter-text");

  const catsOf = (el) =>
    (el.getAttribute("data-filter-cat") || "")
      .split("|")
      .map((s) => s.trim())
      .filter(Boolean);
  const kindOf = (el) => (el.getAttribute("data-filter-kind") || "").trim();

  // Build a dropdown from the values actually present on the page. `values` is
  // the per-item accessor (an array for cat, a single string for kind).
  function fillSelect(sel, values) {
    if (!sel) return;
    const seen = new Set();
    for (const it of items) for (const v of [].concat(values(it))) if (v) seen.add(v);
    if (seen.size === 0) {
      sel.closest(".filter-field").hidden = true;
      return;
    }
    for (const v of Array.from(seen).sort((a, b) =>
      a.toLowerCase().localeCompare(b.toLowerCase())
    )) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      sel.appendChild(o);
    }
  }
  fillSelect(catSel, catsOf);
  fillSelect(kindSel, kindOf);

  function apply() {
    const cat = catSel ? catSel.value : "";
    const kind = kindSel ? kindSel.value : "";
    const from = fromInp ? fromInp.value : "";
    const to = toInp ? toInp.value : "";
    const q = textInp ? textInp.value.trim().toLowerCase() : "";
    let visible = 0;

    for (const it of items) {
      let show = true;
      if (cat && !catsOf(it).includes(cat)) show = false;
      if (show && kind && kindOf(it) !== kind) show = false;
      if (show && (from || to)) {
        const d = it.getAttribute("data-filter-date") || "";
        if (!d || (from && d < from) || (to && d > to)) show = false;
      }
      if (show && q) {
        const hay = (it.getAttribute("data-filter-text") || "").toLowerCase();
        if (!hay.includes(q)) show = false;
      }
      it.hidden = !show;
      if (show) visible++;
    }

    for (const g of groups) g.hidden = !g.querySelector("[data-filter]:not([hidden])");
    for (const e of empties) e.hidden = visible !== 0;
  }

  for (const el of [catSel, kindSel, fromInp, toInp, textInp]) {
    if (!el) continue;
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  }
  const clear = bar.querySelector(".filter-clear");
  if (clear)
    clear.addEventListener("click", () => {
      if (catSel) catSel.value = "";
      if (kindSel) kindSel.value = "";
      if (fromInp) fromInp.value = "";
      if (toInp) toInp.value = "";
      if (textInp) textInp.value = "";
      apply();
    });

  apply();
})();

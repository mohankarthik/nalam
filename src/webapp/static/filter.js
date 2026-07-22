/* Client-side list filter. One bar per page (`.filter-bar`); the rows or
   cards it filters carry `data-filter` plus any of:
     data-filter-cat   -- `|`-joined categories (segment, study type, status...)
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
  const fromInp = bar.querySelector(".filter-from");
  const toInp = bar.querySelector(".filter-to");
  const textInp = bar.querySelector(".filter-text");

  const catsOf = (el) =>
    (el.getAttribute("data-filter-cat") || "")
      .split("|")
      .map((s) => s.trim())
      .filter(Boolean);

  // Build the dropdown from the categories actually present on the page.
  if (catSel) {
    const seen = new Set();
    for (const it of items) for (const c of catsOf(it)) seen.add(c);
    if (seen.size === 0) {
      catSel.closest(".filter-field").hidden = true;
    } else {
      for (const c of Array.from(seen).sort((a, b) =>
        a.toLowerCase().localeCompare(b.toLowerCase())
      )) {
        const o = document.createElement("option");
        o.value = c;
        o.textContent = c;
        catSel.appendChild(o);
      }
    }
  }

  function apply() {
    const cat = catSel ? catSel.value : "";
    const from = fromInp ? fromInp.value : "";
    const to = toInp ? toInp.value : "";
    const q = textInp ? textInp.value.trim().toLowerCase() : "";
    let visible = 0;

    for (const it of items) {
      let show = true;
      if (cat && !catsOf(it).includes(cat)) show = false;
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

  for (const el of [catSel, fromInp, toInp, textInp]) {
    if (!el) continue;
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  }
  const clear = bar.querySelector(".filter-clear");
  if (clear)
    clear.addEventListener("click", () => {
      if (catSel) catSel.value = "";
      if (fromInp) fromInp.value = "";
      if (toInp) toInp.value = "";
      if (textInp) textInp.value = "";
      apply();
    });

  apply();
})();

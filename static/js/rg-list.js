/* rg-list.js — reusable client-side search / facet-filter / sort for internal tables.

   Opt in by wrapping a table in a container with [data-rg-list]. The engine is
   inert on any page that has no such container, so it is safe to load globally.

   Markup contract (all attributes optional except the container + rows):

     <div data-rg-list>
       <input data-rg-search>                         text search box
       <button data-rg-facet="branch" data-rg-value="plantilla">  facet toggle
       <span data-rg-count data-rg-noun="case" data-rg-suffix="in your queue"></span>
       <table>
         <thead>
           <tr><th data-rg-sort data-rg-type="date">Updated</th> ...</tr>
         </thead>
         <tbody>
           <tr data-rg-row data-search="…" data-branch="plantilla">
             <td data-sort-value="2026-07-01T09:00">…</td> ...
           </tr>
         </tbody>
       </table>
       <div data-rg-noresults hidden>No matches.</div>
     </div>

   Rows:   only <tr data-rg-row> participate; others (empty-state rows) are left alone.
   Search: matches against the row's data-search (lowercased), or its text if absent.
   Facets: buttons sharing a data-rg-facet name form one group; data-rg-value "all"/""
           clears that facet. The clicked button gets .is-active.
   Sort:   clicking a [data-rg-sort] header cycles asc → desc → original order. Cell
           value comes from the matching <td data-sort-value> when present, else its text.
           data-rg-type controls comparison: "text" (default), "number", or "date".
*/
(function () {
    "use strict";

    function camel(name) {
        return name.replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
    }

    function rowSearchText(row) {
        return (row.getAttribute("data-search") || row.textContent || "").toLowerCase();
    }

    function init(container) {
        var table = container.querySelector("table");
        if (!table) return;
        var tbody = table.tBodies[0];
        if (!tbody) return;

        var rows = Array.prototype.filter.call(
            tbody.querySelectorAll("tr"),
            function (tr) { return tr.hasAttribute("data-rg-row"); }
        );
        if (!rows.length) return;

        var originalOrder = rows.slice();
        var searchInput = container.querySelector("[data-rg-search]");
        var facetButtons = Array.prototype.slice.call(container.querySelectorAll("[data-rg-facet]"));
        var countEl = container.querySelector("[data-rg-count]");
        var noResultsEl = container.querySelector("[data-rg-noresults]");
        var activeFacets = {};   // facet name -> selected value ("" means all)

        function matches(row) {
            var query = searchInput ? searchInput.value.trim().toLowerCase() : "";
            if (query && rowSearchText(row).indexOf(query) === -1) return false;
            for (var facet in activeFacets) {
                if (!Object.prototype.hasOwnProperty.call(activeFacets, facet)) continue;
                var want = activeFacets[facet];
                if (want && (row.dataset[camel(facet)] || "") !== want) return false;
            }
            return true;
        }

        function render() {
            var visible = 0;
            rows.forEach(function (row) {
                var show = matches(row);
                row.hidden = !show;
                if (show) visible++;
            });
            if (countEl) {
                var noun = countEl.getAttribute("data-rg-noun") || "";
                var suffix = countEl.getAttribute("data-rg-suffix") || "";
                if (noun) {
                    countEl.innerHTML = "<strong>" + visible + "</strong> " + noun +
                        (visible === 1 ? "" : "s") + (suffix ? " " + suffix : "") + ".";
                } else {
                    countEl.textContent = "Showing " + visible + " of " + rows.length;
                }
            }
            if (noResultsEl) noResultsEl.hidden = visible !== 0;
        }

        /* ── Search ── */
        if (searchInput) {
            searchInput.addEventListener("input", render);
        }

        /* ── Facet toggles ── */
        facetButtons.forEach(function (btn) {
            var facet = btn.getAttribute("data-rg-facet");
            var value = btn.getAttribute("data-rg-value") || "";
            if (value === "all") value = "";
            if (btn.classList.contains("is-active")) activeFacets[facet] = value;
            btn.addEventListener("click", function () {
                facetButtons.forEach(function (b) {
                    if (b.getAttribute("data-rg-facet") === facet) b.classList.remove("is-active");
                });
                btn.classList.add("is-active");
                activeFacets[facet] = value;
                render();
            });
        });

        /* ── Sortable headers ── */
        var headers = Array.prototype.slice.call(table.querySelectorAll("th[data-rg-sort]"));
        headers.forEach(function (th) {
            var headerRow = th.parentNode;
            var colIndex = Array.prototype.indexOf.call(headerRow.children, th);
            var type = th.getAttribute("data-rg-type") || "text";
            th.classList.add("rg-sortable");
            th.setAttribute("aria-sort", "none");

            function cellValue(row) {
                var cell = row.children[colIndex];
                if (!cell) return type === "number" || type === "date" ? -Infinity : "";
                var raw = cell.hasAttribute("data-sort-value")
                    ? cell.getAttribute("data-sort-value")
                    : cell.textContent.trim();
                if (type === "number") {
                    var n = parseFloat(raw.replace(/[^0-9.\-]/g, ""));
                    return isNaN(n) ? -Infinity : n;
                }
                if (type === "date") {
                    var t = Date.parse(raw);
                    return isNaN(t) ? -Infinity : t;
                }
                return raw.toLowerCase();
            }

            th.addEventListener("click", function () {
                var current = th.getAttribute("aria-sort");
                var dir = current === "ascending" ? "descending"
                    : current === "descending" ? "none" : "ascending";

                headers.forEach(function (other) {
                    other.setAttribute("aria-sort", "none");
                    other.classList.remove("rg-sorted-asc", "rg-sorted-desc");
                });

                var ordered;
                if (dir === "none") {
                    ordered = originalOrder;
                } else {
                    var factor = dir === "ascending" ? 1 : -1;
                    ordered = rows.slice().sort(function (a, b) {
                        var va = cellValue(a), vb = cellValue(b);
                        if (va < vb) return -1 * factor;
                        if (va > vb) return 1 * factor;
                        return 0;
                    });
                    th.setAttribute("aria-sort", dir);
                    th.classList.add(dir === "ascending" ? "rg-sorted-asc" : "rg-sorted-desc");
                }
                ordered.forEach(function (row) { tbody.appendChild(row); });
            });
        });

        render();
    }

    function boot() {
        document.querySelectorAll("[data-rg-list]").forEach(init);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

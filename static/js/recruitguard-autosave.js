/*
 * RecruitGuard autosave — silent background save for long forms.
 *
 * Usage:
 *   RG.attachAutosave(form, {
 *       indicator: HTMLElement,        // small live-region element to render state into
 *       operationField: "operation",   // hidden-input name that flags save vs finalize
 *       operationValue: "save",        // value to write when autosaving (a draft save)
 *       debounceMs: 1500,              // wait time between last edit and POST
 *       canSave: function() { ... },   // optional gate; return false to skip an autosave
 *   });
 *
 * Behaviour:
 *   - Listens for input + change on the form, debounces, hashes the form data,
 *     and POSTs only when the data has actually changed since the last save.
 *   - Updates the indicator: "Saving…" / "Saved" / "Saved Ns ago" / "Couldn't save — try again".
 *   - Cancels the timer when the user submits the form manually so explicit saves
 *     and autosave don't race.
 *   - Does not interfere with the form's validation or the manual Save button —
 *     it just runs in the background.
 */
(function () {
    "use strict";

    function formDataHash(form, excluded) {
        var data = new FormData(form);
        var parts = [];
        var keys = [];
        data.forEach(function (_, key) { keys.push(key); });
        keys.sort();
        keys.forEach(function (key) {
            if (excluded[key]) return;
            data.getAll(key).forEach(function (value) {
                if (value && typeof value === "object" && "name" in value) {
                    // File input — hash by name + size, content is not auto-saved here.
                    parts.push(key + "=" + value.name + ":" + value.size);
                } else {
                    parts.push(key + "=" + value);
                }
            });
        });
        return parts.join("|");
    }

    function attachAutosave(form, opts) {
        if (!form) return;
        opts = opts || {};
        var indicator = opts.indicator || null;
        var operationField = opts.operationField || "operation";
        var operationValue = opts.operationValue || "save";
        var debounceMs = typeof opts.debounceMs === "number" ? opts.debounceMs : 1500;
        var canSave = typeof opts.canSave === "function" ? opts.canSave : null;

        var excluded = {};
        excluded["csrfmiddlewaretoken"] = true;
        excluded[operationField] = true;

        var lastSavedHash = formDataHash(form, excluded);
        var lastSavedAt = null;
        var debounceTimer = null;
        var refreshTimer = null;
        var inFlight = false;

        function setState(state, message) {
            if (!indicator) return;
            indicator.classList.remove("is-saving", "is-saved", "is-error", "is-idle");
            indicator.classList.add("is-" + state);
            indicator.textContent = message || "";
        }

        function renderTimeAgo() {
            if (!indicator || !lastSavedAt) return;
            if (indicator.classList.contains("is-saving") || indicator.classList.contains("is-error")) return;
            var secs = Math.max(0, Math.floor((Date.now() - lastSavedAt) / 1000));
            var msg;
            if (secs < 5) msg = "Saved";
            else if (secs < 60) msg = "Saved " + secs + "s ago";
            else if (secs < 3600) msg = "Saved " + Math.floor(secs / 60) + "m ago";
            else msg = "Saved " + Math.floor(secs / 3600) + "h ago";
            setState("saved", msg);
        }

        function flush() {
            if (inFlight) {
                // Reschedule a flush right after the current request completes.
                return;
            }
            if (canSave && !canSave()) return;

            var currentHash = formDataHash(form, excluded);
            if (currentHash === lastSavedHash) return;

            var payload = new FormData(form);
            payload.set(operationField, operationValue);

            inFlight = true;
            setState("saving", "Saving…");

            fetch(form.action, {
                method: "POST",
                body: payload,
                credentials: "include",
                redirect: "follow",
                headers: { "X-Requested-With": "RG-Autosave" }
            }).then(function (response) {
                if (!response.ok) throw new Error("status " + response.status);
                lastSavedHash = currentHash;
                lastSavedAt = Date.now();
                setState("saved", "Saved");
            }).catch(function () {
                setState("error", "Couldn’t save — try again");
            }).then(function () {
                inFlight = false;
            });
        }

        function schedule() {
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(flush, debounceMs);
        }

        form.addEventListener("input", schedule);
        form.addEventListener("change", schedule);
        form.addEventListener("submit", function () {
            if (debounceTimer) clearTimeout(debounceTimer);
        });

        if (refreshTimer) clearInterval(refreshTimer);
        refreshTimer = setInterval(renderTimeAgo, 5000);

        setState("idle", "");
    }

    window.RG = window.RG || {};
    window.RG.attachAutosave = attachAutosave;
})();

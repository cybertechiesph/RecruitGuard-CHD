/*
 * RecruitGuard notifications bell (Slice D1)
 * --------------------------------------------
 *  - Toggles the dropdown panel on bell click
 *  - Closes on outside click and Escape
 *  - Polls the unread-count endpoint every 60s and updates the badge
 *  - Keeps the bell accessible (aria-expanded + focus management)
 *
 * The bell wrapper opts in via:
 *   <div data-rg-notif data-rg-notif-poll-url="/internal/notifications/unread-count/">
 */
(function () {
    "use strict";

    var POLL_INTERVAL_MS = 60 * 1000;

    function initBell(root) {
        var toggle = root.querySelector("[data-rg-notif-toggle]");
        var panel = root.querySelector("[data-rg-notif-panel]");
        var badge = root.querySelector("[data-rg-notif-badge]");
        var pollUrl = root.getAttribute("data-rg-notif-poll-url") || "";
        if (!toggle || !panel) return;

        function setOpen(open) {
            if (open) {
                panel.hidden = false;
                toggle.setAttribute("aria-expanded", "true");
                // Move focus to the first interactive thing in the panel for keyboard users
                var firstFocusable = panel.querySelector(
                    "button, a, [tabindex]:not([tabindex='-1'])"
                );
                if (firstFocusable) {
                    // Small delay so the panel paints before focus moves
                    window.setTimeout(function () {
                        try { firstFocusable.focus({ preventScroll: true }); } catch (e) { firstFocusable.focus(); }
                    }, 10);
                }
            } else {
                panel.hidden = true;
                toggle.setAttribute("aria-expanded", "false");
            }
        }

        function isOpen() {
            return toggle.getAttribute("aria-expanded") === "true";
        }

        toggle.addEventListener("click", function (e) {
            e.stopPropagation();
            setOpen(!isOpen());
        });

        /* Close on outside click */
        document.addEventListener("click", function (e) {
            if (!isOpen()) return;
            if (root.contains(e.target)) return;
            setOpen(false);
        });

        /* Close on Escape and return focus to bell */
        document.addEventListener("keydown", function (e) {
            if (e.key !== "Escape" || !isOpen()) return;
            setOpen(false);
            try { toggle.focus({ preventScroll: true }); } catch (err) { toggle.focus(); }
        });

        /* Badge polling */
        function applyCount(count) {
            if (!badge) return;
            if (typeof count !== "number" || count <= 0) {
                badge.classList.add("is-hidden");
                badge.textContent = "";
                toggle.setAttribute("aria-label", "Notifications");
                return;
            }
            var label = count > 9 ? "9+" : String(count);
            badge.classList.remove("is-hidden");
            badge.textContent = label;
            toggle.setAttribute(
                "aria-label",
                "Notifications, " + count + " unread"
            );
        }

        function poll() {
            if (!pollUrl) return;
            // Skip polling while the dropdown is open — the user has the current list
            if (isOpen()) return;
            fetch(pollUrl, {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
            })
                .then(function (res) { return res.ok ? res.json() : null; })
                .then(function (data) {
                    if (data && typeof data.count === "number") {
                        applyCount(data.count);
                    }
                })
                .catch(function () { /* swallow — next tick retries */ });
        }

        if (pollUrl) {
            window.setInterval(poll, POLL_INTERVAL_MS);
            // Also refresh when the tab regains focus
            document.addEventListener("visibilitychange", function () {
                if (document.visibilityState === "visible") poll();
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        var bells = document.querySelectorAll("[data-rg-notif]");
        bells.forEach(initBell);
    });
})();

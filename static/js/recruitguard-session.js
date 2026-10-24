/*
 * Internal-portal inactivity warning (TI-1).
 *
 * The server signs a user out after SESSION_INACTIVITY_TIMEOUT_SECONDS without a request
 * that extends the session (background polls do not count). This script mirrors that
 * timer in the browser so the sign-out is announced instead of discovered on the next save:
 *
 *   - The last time the session was extended is shared across tabs in localStorage, so
 *     working in one tab keeps the others from warning or signing out.
 *   - Page loads, successful autosaves ("rg:session-activity") and keep-alive pings reset it.
 *   - While the user types or clicks on a page that makes no requests of its own, a
 *     keep-alive is sent at most once every KEEPALIVE_MIN_GAP_MS.
 *   - WARNING seconds before expiry an alert dialog offers "Stay signed in" / "Sign out".
 *   - At zero the page posts the logout form and lands on the sign-in page with
 *     ?reason=timeout, keeping the current page as ?next= for after re-authentication.
 *
 * Markup contract (internal_base.html): a [data-rg-session] element carrying
 * data-timeout-seconds, data-warning-seconds, data-keepalive-url, data-login-url and
 * data-session-check-url, containing the dialog and a hidden logout <form>.
 */
(function () {
    "use strict";

    var STORAGE_KEY = "rg-session-last-activity";
    var KEEPALIVE_MIN_GAP_MS = 5 * 60 * 1000;

    var root = document.querySelector("[data-rg-session]");
    if (!root) return;

    var timeoutMs = parseInt(root.getAttribute("data-timeout-seconds"), 10) * 1000;
    var warningMs = parseInt(root.getAttribute("data-warning-seconds"), 10) * 1000;
    var keepaliveUrl = root.getAttribute("data-keepalive-url");
    var loginUrl = root.getAttribute("data-login-url");
    if (!timeoutMs || !keepaliveUrl) return;
    if (!warningMs || warningMs >= timeoutMs) warningMs = Math.min(120000, Math.floor(timeoutMs / 4));

    var dialogEl = root.querySelector("[data-rg-session-dialog]");
    var countdownEl = root.querySelector("[data-rg-session-countdown]");
    var stayBtn = root.querySelector("[data-rg-session-stay]");
    var signOutBtn = root.querySelector("[data-rg-session-signout]");
    var logoutForm = root.querySelector("[data-rg-session-logout-form]");
    var csrfInput = logoutForm && logoutForm.querySelector("input[name='csrfmiddlewaretoken']");

    var modal = null;
    var warningShown = false;
    var ended = false;
    var pinging = false;
    var lastPingAt = Date.now();
    var localActivity = Date.now();

    function readShared() {
        try {
            var v = parseInt(localStorage.getItem(STORAGE_KEY), 10);
            return isNaN(v) ? 0 : v;
        } catch (e) { return 0; }
    }
    function lastActivity() { return Math.max(localActivity, readShared()); }
    function markActivity(at) {
        localActivity = Math.max(localActivity, at || Date.now());
        try { localStorage.setItem(STORAGE_KEY, String(localActivity)); } catch (e) {}
    }

    function fmt(ms) {
        var total = Math.max(0, Math.ceil(ms / 1000));
        var m = Math.floor(total / 60);
        var s = total % 60;
        return m + ":" + (s < 10 ? "0" : "") + s;
    }

    function showWarning(remaining) {
        warningShown = true;
        if (countdownEl) countdownEl.textContent = fmt(remaining);
        if (dialogEl && window.bootstrap && window.bootstrap.Modal) {
            modal = modal || new window.bootstrap.Modal(dialogEl, { backdrop: "static", keyboard: false });
            modal.show();
        }
    }
    function hideWarning() {
        warningShown = false;
        if (modal) modal.hide();
    }

    function signOut(reasonTimeout) {
        if (ended) return;
        ended = true;
        if (!logoutForm) {
            window.location.href = loginUrl;
            return;
        }
        var here = window.location.pathname + window.location.search;
        var target = loginUrl + "?next=" + encodeURIComponent(here);
        if (reasonTimeout) target += "&reason=timeout";
        var nextInput = logoutForm.querySelector("input[name='next']");
        if (nextInput) nextInput.value = target;
        logoutForm.submit();
    }

    function keepAlive() {
        if (pinging || ended) return Promise.resolve(false);
        pinging = true;
        lastPingAt = Date.now();
        var body = new FormData();
        if (csrfInput) body.append("csrfmiddlewaretoken", csrfInput.value);
        return fetch(keepaliveUrl, {
            method: "POST",
            body: body,
            credentials: "same-origin",
            redirect: "manual",
            headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" }
        }).then(function (res) {
            if (res.status === 200) {
                markActivity(Date.now());
                return true;
            }
            return false;
        }).catch(function () {
            return false;
        }).then(function (ok) {
            pinging = false;
            return ok;
        });
    }

    function tick() {
        if (ended) return;
        var remaining = timeoutMs - (Date.now() - lastActivity());
        if (remaining <= 0) {
            signOut(true);
            return;
        }
        if (remaining <= warningMs) {
            if (!warningShown) showWarning(remaining);
            else if (countdownEl) countdownEl.textContent = fmt(remaining);
        } else if (warningShown) {
            // Another tab (or an autosave) extended the session.
            hideWarning();
        }
    }

    function onUserInput() {
        if (ended || warningShown) return;
        if (Date.now() - lastPingAt < KEEPALIVE_MIN_GAP_MS) return;
        keepAlive();
    }

    // The page load itself extended the session.
    markActivity(Date.now());

    document.addEventListener("rg:session-activity", function () { markActivity(Date.now()); });
    document.addEventListener("rg:session-ended", function () {
        // Autosave already told the user their change was not saved; don't yank the page away.
        ended = true;
        hideWarning();
    });
    ["keydown", "pointerdown", "input"].forEach(function (type) {
        document.addEventListener(type, onUserInput, { passive: true, capture: true });
    });
    if (stayBtn) {
        stayBtn.addEventListener("click", function () {
            stayBtn.disabled = true;
            keepAlive().then(function (ok) {
                stayBtn.disabled = false;
                if (ok) hideWarning();
                else signOut(true);
            });
        });
    }
    if (signOutBtn) {
        signOutBtn.addEventListener("click", function () { signOut(false); });
    }
    window.addEventListener("storage", function (e) {
        if (e.key === STORAGE_KEY) tick();
    });
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") tick();
    });

    window.setInterval(tick, 1000);
})();

/* RecruitGuard — shared wizard error display
 * ---------------------------------------------------------------------------
 * One implementation of the "show every problem at once, inline below its
 * field" pattern, shared by the internal staff wizards (screening, exam,
 * decision, completion, final selection, interview session) and mirroring the
 * applicant intake form's behaviour.
 *
 * A wizard collects problems as an array of:
 *     { field, msg, anchor }
 *   - field  : the control to focus + flag with aria-invalid (select, textarea,
 *              or a choice-strip button).
 *   - msg    : plain-English message.
 *   - anchor : OPTIONAL element to insert the error node *after*. Defaults to
 *              `field`. Use this when the field sits inside a wrapper that the
 *              error should follow instead (e.g. a choice strip of buttons —
 *              anchor on the strip container, not one button).
 *
 * Then it calls:
 *     RGWizardErrors.showErrors(problems, { scope, summaryBox });
 *
 * `scope`      : root to clear previous errors within (defaults to document).
 * `summaryBox` : OPTIONAL element (ideally role="alert") that receives a short
 *                count summary so screen-reader users hear that something
 *                failed; inline nodes carry the specifics.
 * ------------------------------------------------------------------------- */
(function (global) {
    "use strict";

    function clearFieldErrors(scope) {
        scope = scope || document;
        Array.prototype.forEach.call(
            scope.querySelectorAll("[data-wiz-error]"),
            function (node) { node.remove(); }
        );
        Array.prototype.forEach.call(
            scope.querySelectorAll("[data-wiz-invalid]"),
            function (el) {
                el.classList.remove("is-invalid");
                el.removeAttribute("aria-invalid");
                el.removeAttribute("data-wiz-invalid");
                if (el.hasAttribute("data-wiz-orig-describedby")) {
                    var orig = el.getAttribute("data-wiz-orig-describedby");
                    if (orig) { el.setAttribute("aria-describedby", orig); }
                    else { el.removeAttribute("aria-describedby"); }
                    el.removeAttribute("data-wiz-orig-describedby");
                }
            }
        );
    }

    function renderInlineError(field, msg, idx, anchor) {
        anchor = anchor || field;
        if (!anchor || !anchor.parentNode) return;
        var errId = "rg-wiz-err-" + idx;
        var node = document.createElement("div");
        node.className = "invalid-feedback d-block rg-wiz-error";
        node.id = errId;
        node.setAttribute("data-wiz-error", "");
        node.textContent = msg;
        anchor.parentNode.insertBefore(node, anchor.nextSibling);

        if (field && field.setAttribute) {
            field.setAttribute("aria-invalid", "true");
            field.setAttribute("data-wiz-invalid", "");
            if (field.classList) { field.classList.add("is-invalid"); }
            if (!field.hasAttribute("data-wiz-orig-describedby")) {
                field.setAttribute(
                    "data-wiz-orig-describedby",
                    field.getAttribute("aria-describedby") || ""
                );
            }
            var prior = field.getAttribute("aria-describedby");
            field.setAttribute("aria-describedby", prior ? prior + " " + errId : errId);
        }
    }

    function showErrors(problems, opts) {
        opts = opts || {};
        var scope = opts.scope || document;
        clearFieldErrors(scope);
        if (opts.summaryBox) {
            opts.summaryBox.hidden = true;
            opts.summaryBox.textContent = "";
        }
        if (!problems || !problems.length) return;

        var firstFocusable = null;
        var inlineCount = 0;
        var summaryMsgs = [];
        problems.forEach(function (pr, i) {
            var field = pr.field;
            // Choice-strip buttons must not split the strip — anchor the error
            // on the radiogroup container. Explicit pr.anchor always wins.
            var stripAnchor = field && field.closest
                ? field.closest("[role='radiogroup']")
                : null;
            var anchor = pr.anchor || stripAnchor || field;
            // summaryOnly: no clean visible anchor (e.g. a hidden <select> with
            // its own row highlight) — route the message to the summary box.
            if (!pr.summaryOnly && anchor && anchor.nodeType === 1) {
                renderInlineError(field, pr.msg, i, anchor);
                inlineCount += 1;
            } else if (summaryMsgs.indexOf(pr.msg) === -1) {
                summaryMsgs.push(pr.msg);
            }
            // Only ever focus something the user can actually see/reach. A hidden
            // <select> (offsetParent === null) is skipped; we fall back to the
            // summary box below.
            if (!firstFocusable && isFocusable(field)) { firstFocusable = field; }
        });

        if (opts.summaryBox) {
            var parts = [];
            if (summaryMsgs.length) { parts.push(summaryMsgs.join(" ")); }
            if (inlineCount) {
                // When summary-only messages coexist with inline ones, the alert
                // must still tell the user there are highlighted items below.
                var items = inlineCount === 1 ? "highlighted item" : inlineCount + " highlighted items";
                parts.push(summaryMsgs.length
                    ? "Also fix the " + items + " below."
                    : "Please fix the " + items + " below.");
            }
            opts.summaryBox.textContent = parts.join(" ");
            opts.summaryBox.hidden = !opts.summaryBox.textContent;
        }

        if (typeof opts.onShow === "function") { opts.onShow(problems); }

        // Focus the first reachable field; if there is none (all errors are
        // summary-only on hidden controls), move focus to the alert box instead.
        var focusTarget = firstFocusable;
        if (!focusTarget && opts.summaryBox && opts.summaryBox.textContent) {
            if (!opts.summaryBox.hasAttribute("tabindex")) {
                opts.summaryBox.setAttribute("tabindex", "-1");
            }
            focusTarget = opts.summaryBox;
        }
        if (focusTarget && typeof focusTarget.focus === "function") {
            try { focusTarget.focus({ preventScroll: false }); }
            catch (e) { focusTarget.focus(); }
        }
    }

    function isFocusable(el) {
        // Visible + focusable. offsetParent is null for display:none / detached
        // nodes (good enough for our wizards; none use position:fixed fields).
        return !!(el && el.nodeType === 1 && typeof el.focus === "function" && el.offsetParent !== null);
    }

    global.RGWizardErrors = {
        clearFieldErrors: clearFieldErrors,
        renderInlineError: renderInlineError,
        showErrors: showErrors
    };
})(window);

(function () {
    "use strict";

    function scorePassword(value) {
        var score = 0;
        if (value.length >= 8) score += 1;
        if (value.length >= 12) score += 1;
        if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1;
        if (/\d/.test(value)) score += 1;
        if (/[^A-Za-z0-9]/.test(value)) score += 1;
        return Math.min(score, 4);
    }

    function labelForScore(score, hasValue) {
        if (!hasValue) return "Password strength";
        if (score <= 1) return "Weak";
        if (score === 2) return "Fair";
        if (score === 3) return "Good";
        return "Strong";
    }

    function classForScore(score, hasValue) {
        if (!hasValue) return "bg-secondary";
        if (score <= 1) return "bg-danger";
        if (score === 2) return "bg-warning";
        if (score === 3) return "bg-info";
        return "bg-success";
    }

    function attachMeter(input) {
        if (input.dataset.passwordStrengthReady === "true") return;
        input.dataset.passwordStrengthReady = "true";

        var wrapper = document.createElement("div");
        wrapper.className = "rg-password-strength mt-2";
        wrapper.innerHTML = [
            '<div class="progress" style="height:0.45rem;">',
            '<div class="progress-bar bg-secondary" role="progressbar" style="width:0%;" ',
            'aria-valuemin="0" aria-valuemax="4" aria-valuenow="0"></div>',
            "</div>",
            '<div class="form-text rg-password-strength__label">Password strength</div>'
        ].join("");

        input.insertAdjacentElement("afterend", wrapper);

        var bar = wrapper.querySelector(".progress-bar");
        var label = wrapper.querySelector(".rg-password-strength__label");

        function refresh() {
            var hasValue = input.value.length > 0;
            var score = scorePassword(input.value);
            bar.className = "progress-bar " + classForScore(score, hasValue);
            bar.style.width = hasValue ? ((score + 1) * 20) + "%" : "0%";
            bar.setAttribute("aria-valuenow", String(score));
            label.textContent = labelForScore(score, hasValue);
        }

        input.addEventListener("input", refresh);
        refresh();
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-password-strength='true']").forEach(attachMeter);
    });
})();

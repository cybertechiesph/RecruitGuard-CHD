/*
 * Live "time left" for applicant deadlines. Any element with data-closes-at (an ISO date-time)
 * shows the hours and minutes left, updated every 30 seconds. The server stays the authority:
 * a late submit is refused there, whatever this shows.
 */
(function () {
    "use strict";
    var nodes = document.querySelectorAll("[data-closes-at]");
    Array.prototype.forEach.call(nodes, function (node) {
        var closesAt = new Date(node.getAttribute("data-closes-at")).getTime();
        if (isNaN(closesAt)) return;
        function tick() {
            var left = closesAt - Date.now();
            if (left <= 0) {
                node.textContent = "Applications are now closed.";
                return false;
            }
            var hours = Math.floor(left / 3600000);
            var minutes = Math.floor((left % 3600000) / 60000);
            node.textContent = (hours ? hours + " hr " : "") + minutes + " min left";
            return true;
        }
        if (tick()) {
            var timer = setInterval(function () { if (!tick()) clearInterval(timer); }, 30000);
        }
    });
})();

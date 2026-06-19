/**
 * Incremental "In step" timer tick for Live Status.
 *
 * The server renders each timer element with:
 *   class="step-elapsed" and data-updated-at="<ISO UTC timestamp>"
 *
 * This function runs every second via a Dash clientside callback and updates
 * only timer text in place, so users get smooth increments without waiting for
 * a server round-trip.
 */
(function () {
    function formatElapsed(totalSeconds) {
        var s = Math.max(0, Math.floor(totalSeconds));
        if (s < 60) return s + "s";
        var mins = Math.floor(s / 60);
        var secs = s % 60;
        if (mins < 60) return mins + "m " + String(secs).padStart(2, "0") + "s";
        var hours = Math.floor(mins / 60);
        mins = mins % 60;
        if (hours < 24) return hours + "h " + String(mins).padStart(2, "0") + "m";
        var days = Math.floor(hours / 24);
        hours = hours % 24;
        return days + "d " + hours + "h";
    }

    window.equipmentMonitorLiveTimerTick = function equipmentMonitorLiveTimerTick() {
        var nowMs = Date.now();
        var nodes = document.querySelectorAll(".step-elapsed[data-updated-at]");
        nodes.forEach(function (node) {
            var raw = node.getAttribute("data-updated-at");
            if (!raw) return;
            var updatedMs = Date.parse(raw);
            if (Number.isNaN(updatedMs)) return;
            var elapsed = Math.max(0, Math.floor((nowMs - updatedMs) / 1000));
            node.textContent = "In step: " + formatElapsed(elapsed);
        });
    };
})();

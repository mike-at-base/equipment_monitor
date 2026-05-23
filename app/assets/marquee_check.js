/**
 * marquee_check.js
 *
 * After each Dash render, inspect every .step-desc-outer element.
 * If its inner span overflows the container width, set --scroll-dist and
 * add the "marquee-active" class to trigger the CSS scroll animation.
 * Otherwise, remove the class so the text just sits static.
 *
 * Uses a MutationObserver on document.body so it fires automatically
 * whenever Dash swaps in new card content (every 5 s on the status interval).
 */

function checkDescOverflow() {
    document.querySelectorAll('.step-desc-outer').forEach(function (outer) {
        var inner = outer.querySelector('.step-desc-inner');
        if (!inner) return;

        // Temporarily clear animation so scrollWidth reflects natural text width
        inner.classList.remove('marquee-active');

        var overflow = inner.scrollWidth - outer.offsetWidth;
        if (overflow > 2) {                          // 2 px tolerance for sub-pixel
            inner.style.setProperty('--scroll-dist', -overflow + 'px');
            inner.classList.add('marquee-active');
        } else {
            inner.style.removeProperty('--scroll-dist');
        }
    });
}

// Watch for Dash re-renders (Dash replaces child nodes in the layout container)
var _marqueeObserver = new MutationObserver(function (mutations) {
    var relevant = mutations.some(function (m) { return m.addedNodes.length > 0; });
    if (relevant) {
        // Small delay so the DOM is fully painted before we measure
        setTimeout(checkDescOverflow, 120);
    }
});

document.addEventListener('DOMContentLoaded', function () {
    _marqueeObserver.observe(document.body, { childList: true, subtree: true });
    // Initial check once layout is painted
    setTimeout(checkDescOverflow, 300);
});

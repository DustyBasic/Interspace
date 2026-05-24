/* Interspace theme toggle.
 *
 * Reads/writes localStorage["interspace.theme"] ∈ {"light", "dark"}.
 * Respects prefers-color-scheme as the initial default when no preference set.
 * Theme is applied via data-theme attribute on <html> so CSS variables swap.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "interspace.theme";

  function currentTheme() {
    try {
      var t = localStorage.getItem(STORAGE_KEY);
      if (t === "dark" || t === "light") return t;
    } catch (e) {}
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function applyTheme(theme) {
    if (theme === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function saveTheme(theme) {
    try { localStorage.setItem(STORAGE_KEY, theme); } catch (e) {}
  }

  function init() {
    applyTheme(currentTheme());

    var btn = document.getElementById("theme-toggle");
    if (!btn) return;

    btn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      saveTheme(next);
      applyTheme(next);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

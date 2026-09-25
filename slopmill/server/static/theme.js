/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Runs before the page draws, so a saved theme never flashes the other one. A file, not an
   inline script: the page's Content-Security-Policy allows no inline script at all. */
(function () {
  var t = "auto";
  try { t = localStorage.getItem("cmp-theme") || "auto"; } catch (e) {}
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
})();

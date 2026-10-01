/* Shared appearance preference. Run in <head> before the first paint. */
(function () {
  "use strict";
  var key = "bss.appearance";
  var root = document.documentElement;
  function valid(value) {
    return value === "light" || value === "dark" ? value : "system";
  }
  function read() {
    try { return valid(localStorage.getItem(key) || root.getAttribute("data-appearance-default")); }
    catch (_) { return valid(root.getAttribute("data-appearance-default")); }
  }
  function apply(value) {
    value = valid(value);
    if (value === "system") root.removeAttribute("data-appearance");
    else root.setAttribute("data-appearance", value);
    document.querySelectorAll("[data-appearance-picker]").forEach(function (picker) {
      picker.value = value;
    });
  }
  apply(read());
  document.addEventListener("DOMContentLoaded", function () { apply(read()); });
  document.addEventListener("change", function (event) {
    if (!event.target.matches("[data-appearance-picker]")) return;
    var value = valid(event.target.value);
    apply(value);
    try { localStorage.setItem(key, value); } catch (_) { /* Session-only choice. */ }
  });
  window.addEventListener("storage", function (event) {
    if (event.key === key || event.key === null) apply(read());
  });
})();

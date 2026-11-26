/* ============================================================
   Linkora · 灵桥 — 交互脚本
   主题三态切换（默认跟随系统）/ 滚动进场 / 移动菜单 / 数字计数
   注意：不使用任何模板语法，避免 GitHub Pages 构建失败。
   ============================================================ */
(function () {
  "use strict";

  var root = document.documentElement;
  var STORE = "linkora-theme";
  root.classList.add("js");

  function currentPref() {
    var p = null;
    try { p = localStorage.getItem(STORE); } catch (e) {}
    if (p !== "light" && p !== "dark" && p !== "system") p = "system";
    return p;
  }

  function applyTheme(pref) {
    var dark = pref === "dark" ||
      (pref === "system" &&
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.setAttribute("data-pref", pref);
  }

  function syncToggle() {
    var pref = currentPref();
    var seg = document.getElementById("themeSeg");
    if (!seg) return;
    seg.querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-value") === pref ? "true" : "false");
    });
  }

  /* ---- 初始化主题（首帧已防闪，这里再同步一次） ---- */
  applyTheme(currentPref());
  syncToggle();

  /* ---- 主题切换：分段按钮 ---- */
  var seg = document.getElementById("themeSeg");
  if (seg) {
    seg.querySelectorAll("button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var next = btn.getAttribute("data-value");
        try { localStorage.setItem(STORE, next); } catch (e) {}
        applyTheme(next);
        syncToggle();
      });
    });
  }

  /* 系统主题变化时，若处于 system 态则实时跟随 */
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var handler = function () { if (currentPref() === "system") applyTheme("system"); };
    if (mq.addEventListener) mq.addEventListener("change", handler);
    else if (mq.addListener) mq.addListener(handler);
  }

  /* ---- 导航滚动玻璃态 ---- */
  var nav = document.querySelector(".nav");
  function onScroll() {
    if (!nav) return;
    if (window.scrollY > 20) nav.classList.add("scrolled");
    else nav.classList.remove("scrolled");
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---- 移动菜单 ---- */
  var burger = document.getElementById("burger");
  var menu = document.getElementById("mobileMenu");
  function closeMenu() {
    if (burger) burger.classList.remove("open");
    if (menu) menu.classList.remove("open");
  }
  if (burger && menu) {
    burger.addEventListener("click", function () {
      burger.classList.toggle("open");
      menu.classList.toggle("open");
    });
    menu.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", closeMenu);
    });
  }

  /* ---- 滚动进场 (IntersectionObserver) ---- */
  var reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && reveals.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          en.target.classList.add("in");
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("in"); });
  }

  /* ---- 数字计数 ---- */
  function animateCount(el) {
    var target = parseFloat(el.getAttribute("data-count")) || 0;
    var dur = 1400;
    var start = performance.now();
    function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - t, 3);
      var val = Math.round(target * eased);
      el.textContent = val + (el.getAttribute("data-suffix") || "");
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  var counters = document.querySelectorAll(".stat__num[data-count]");
  if ("IntersectionObserver" in window && counters.length) {
    var co = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          animateCount(en.target);
          co.unobserve(en.target);
        }
      });
    }, { threshold: 0.5 });
    counters.forEach(function (el) { co.observe(el); });
  } else {
    counters.forEach(function (el) {
      el.textContent = (el.getAttribute("data-count") || "") + (el.getAttribute("data-suffix") || "");
    });
  }

})();

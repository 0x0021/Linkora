/* ============================================================
   Linkora · 灵桥 — 交互脚本
   主题三态切换 / 滚动进场 / 磁性按钮 / 导航玻璃态 /
   移动菜单 / 数字计数 / 轻量粒子背景
   注意：不使用任何模板语法，避免 GitHub Pages 构建失败。
   ============================================================ */
(function () {
  "use strict";

  var root = document.documentElement;
  var STORE = "linkora-theme";

  /* ---- 图标 ---- */
  var ICON = {
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M4.2 4.2l1.7 1.7M18.1 18.1l1.7 1.7M2 12h2.4M19.6 12H22M4.2 19.8l1.7-1.7M18.1 5.9l1.7-1.7"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/></svg>',
    system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>'
  };

  var ORDER = ["light", "dark", "system"];

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
    var tg = document.getElementById("themeToggle");
    if (!tg) return;
    tg.setAttribute("data-state", pref);
    var thumb = tg.querySelector(".theme-toggle__thumb");
    if (thumb) thumb.innerHTML = ICON[pref] || ICON.system;
  }

  /* ---- 初始化主题（首帧已防闪，这里再同步一次） ---- */
  applyTheme(currentPref());
  syncToggle();

  /* ---- 主题切换：三态循环 ---- */
  var tg = document.getElementById("themeToggle");
  if (tg) {
    tg.addEventListener("click", function () {
      var cur = currentPref();
      var next = ORDER[(ORDER.indexOf(cur) + 1) % ORDER.length];
      try { localStorage.setItem(STORE, next); } catch (e) {}
      applyTheme(next);
      syncToggle();
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

  /* ---- 磁性按钮 ---- */
  var mags = document.querySelectorAll(".btn--magnetic");
  mags.forEach(function (btn) {
    btn.addEventListener("mousemove", function (e) {
      var r = btn.getBoundingClientRect();
      var mx = e.clientX - r.left - r.width / 2;
      var my = e.clientY - r.top - r.height / 2;
      btn.style.transform = "translate(" + mx * 0.18 + "px," + my * 0.28 + "px)";
    });
    btn.addEventListener("mouseleave", function () {
      btn.style.transform = "";
    });
  });

  /* ---- 特性卡鼠标跟随高亮 ---- */
  document.querySelectorAll(".feature-card").forEach(function (card) {
    card.addEventListener("mousemove", function (e) {
      var r = card.getBoundingClientRect();
      card.style.setProperty("--mx", (e.clientX - r.left) + "px");
      card.style.setProperty("--my", (e.clientY - r.top) + "px");
    });
  });

  /* ---- 轻量粒子背景 ---- */
  var canvas = document.getElementById("heroCanvas");
  if (canvas) {
    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var fine = window.matchMedia &&
      window.matchMedia("(pointer: fine)").matches;

    if (!reduce && fine) {
      var ctx = canvas.getContext("2d");
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var W = 0, H = 0;
      var pts = [];

      function resize() {
        var rect = canvas.getBoundingClientRect();
        W = rect.width; H = rect.height;
        canvas.width = Math.max(1, Math.floor(W * dpr));
        canvas.height = Math.max(1, Math.floor(H * dpr));
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        var count = Math.max(20, Math.min(46, Math.floor(W / 26)));
        pts = [];
        for (var i = 0; i < count; i++) {
          pts.push({
            x: Math.random() * W,
            y: Math.random() * H,
            vx: (Math.random() - 0.5) * 0.25,
            vy: -0.12 - Math.random() * 0.28,
            r: 1 + Math.random() * 1.8,
            c: Math.random() > 0.5 ? "99,102,241" : "34,211,238"
          });
        }
      }

      function tick() {
        if (document.hidden) { raf = 0; return; }
        ctx.clearRect(0, 0, W, H);
        for (var i = 0; i < pts.length; i++) {
          var p = pts[i];
          p.x += p.vx; p.y += p.vy;
          if (p.y < -10) { p.y = H + 10; p.x = Math.random() * W; }
          if (p.x < -10) p.x = W + 10;
          if (p.x > W + 10) p.x = -10;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fillStyle = "rgba(" + p.c + ",0.55)";
          ctx.fill();
        }
        // 近点连线
        for (var a = 0; a < pts.length; a++) {
          for (var b = a + 1; b < pts.length; b++) {
            var dx = pts[a].x - pts[b].x;
            var dy = pts[a].y - pts[b].y;
            var d2 = dx * dx + dy * dy;
            if (d2 < 13000) {
              var al = (1 - d2 / 13000) * 0.18;
              ctx.strokeStyle = "rgba(129,140,248," + al.toFixed(3) + ")";
              ctx.lineWidth = 1;
              ctx.beginPath();
              ctx.moveTo(pts[a].x, pts[a].y);
              ctx.lineTo(pts[b].x, pts[b].y);
              ctx.stroke();
            }
          }
        }
        raf = requestAnimationFrame(tick);
      }

      var raf = 0;
      function start() { if (!raf) raf = requestAnimationFrame(tick); }

      resize();
      start();
      window.addEventListener("resize", resize);
      document.addEventListener("visibilitychange", function () {
        if (document.hidden) { if (raf) { cancelAnimationFrame(raf); raf = 0; } }
        else start();
      });
    }
  }
})();

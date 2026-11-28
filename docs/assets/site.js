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

  /* ---- 工作方式：消息沿时间线逐站流转 ---- */
  var shell = document.querySelector("[data-pipeline]");
  if (shell) {
    var stage = shell.querySelector(".flow-stage");
    var lane = shell.querySelector(".flow-lane");
    var dotsLayer = shell.querySelector(".flow-lane__dots");
    var line = shell.querySelector(".flow-lane__line");
    var fill = shell.querySelector(".flow-lane__fill");
    var carrier = shell.querySelector(".flow-carrier");
    var rail = shell.querySelector(".pipeline");
    var nodes = Array.prototype.slice.call(rail ? rail.querySelectorAll(".node") : []);
    if (stage && lane && dotsLayer && line && fill && carrier && nodes.length) {
      var LINE_X = 26;
      var CARRIER_X = 64;
      var dots = nodes.map(function () {
        var d = document.createElement("span");
        d.className = "flow-dot";
        dotsLayer.appendChild(d);
        return d;
      });
      var selected = 0;
      var centers = [];
      var timer = null;
      var paused = false;
      var visible = false;
      var reduceMotion = !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      nodes.forEach(function (n, i) {
        n.setAttribute("role", "listitem");
        n.setAttribute("tabindex", "0");
        if (i === 0) n.setAttribute("aria-current", "step");
      });

      function layout() {
        var sr = stage.getBoundingClientRect();
        centers = nodes.map(function (n, i) {
          var icon = n.querySelector(".node__icon") || n;
          var ir = icon.getBoundingClientRect();
          var y = ir.top + ir.height / 2 - sr.top;
          dots[i].style.left = LINE_X + "px";
          dots[i].style.top = y + "px";
          return { x: LINE_X, y: y };
        });
        var first = centers[0];
        var last = centers[centers.length - 1];
        line.style.left = (LINE_X - 1) + "px";
        line.style.top = first.y + "px";
        line.style.width = "2px";
        line.style.height = (last.y - first.y) + "px";
      }

      function place(i, instant) {
        var c = centers[i];
        if (!c) return;
        if (instant) carrier.style.transition = "none";
        carrier.style.transform = "translate(" + (CARRIER_X - carrier.offsetWidth / 2) + "px," + (c.y - carrier.offsetHeight / 2) + "px)";
        if (instant) {
          void carrier.offsetWidth;
          carrier.style.transition = "";
        }
        fill.style.width = "100%";
        fill.style.height = (nodes.length === 1 ? 0 : (i / (nodes.length - 1)) * 100) + "%";
        dots.forEach(function (d, j) {
          d.classList.toggle("is-done", j < i);
          d.classList.toggle("is-active", j === i);
        });
      }

      function select(i, instant) {
        if (i < 0 || i >= nodes.length) return;
        selected = i;
        nodes.forEach(function (n, j) {
          n.classList.toggle("is-active", j === i);
          n.classList.toggle("is-done", j < i);
          if (j === i) n.setAttribute("aria-current", "step");
          else n.removeAttribute("aria-current");
        });
        place(i, instant);
      }

      function stopAuto() {
        if (timer) {
          window.clearInterval(timer);
          timer = null;
        }
      }

      function startAuto() {
        if (reduceMotion || paused || !visible || timer) return;
        timer = window.setInterval(function () {
          if (selected < nodes.length - 1) {
            select(selected + 1, false);
            return;
          }
          carrier.classList.add("is-hidden");
          fill.style.opacity = "0";
          stopAuto();
          window.setTimeout(function () {
            select(0, true);
            carrier.classList.remove("is-hidden");
            fill.style.opacity = "";
            startAuto();
          }, 420);
        }, 2600);
      }

      nodes.forEach(function (n, i) {
        n.addEventListener("click", function () { select(i, false); });
        n.addEventListener("keydown", function (e) {
          var next = -1;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % nodes.length;
          else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + nodes.length) % nodes.length;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = nodes.length - 1;
          if (next >= 0) {
            e.preventDefault();
            select(next, false);
            nodes[next].focus();
          }
        });
        n.addEventListener("pointerenter", function () {
          paused = true;
          stopAuto();
        });
      });

      shell.addEventListener("pointerleave", function () {
        paused = false;
        startAuto();
      });
      shell.addEventListener("focusin", function () {
        paused = true;
        stopAuto();
      });
      shell.addEventListener("focusout", function () {
        if (!shell.contains(document.activeElement)) {
          paused = false;
          startAuto();
        }
      });

      shell.classList.add("is-enhanced");
      layout();
      select(0, true);

      var resizeTimer = null;
      window.addEventListener("resize", function () {
        if (resizeTimer) window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(function () {
          layout();
          place(selected, true);
        }, 120);
      });

      if ("IntersectionObserver" in window && !reduceMotion) {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) {
            visible = en.isIntersecting;
            if (visible) startAuto();
            else stopAuto();
          });
        }, { threshold: 0.25 });
        io.observe(shell);
      }
    }
  }
})();

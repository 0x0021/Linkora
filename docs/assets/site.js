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

  /* ---- 工作方式：五步动态展示 ---- */
  var shell = document.querySelector("[data-pipeline]");
  if (shell) {
    var rail = shell.querySelector(".pipeline");
    var nodes = Array.prototype.slice.call(rail ? rail.querySelectorAll(".node") : []);
    var detail = shell.querySelector(".pipeline__detail");
    var fill = shell.querySelector(".pipeline__progress-fill");
    if (nodes.length && detail && fill) {
      var steps = nodes.map(function (n) {
        var h = n.querySelector("h3");
        var p = n.querySelector("p");
        return {
          title: h ? h.textContent.trim() : "",
          desc: p ? p.textContent.trim() : ""
        };
      });
      var detailStep = detail.querySelector(".pipeline__detail-step");
      var detailTitle = detail.querySelector(".pipeline__detail-copy h3");
      var detailDesc = detail.querySelector(".pipeline__detail-copy p");
      var selected = 0;
      var timer = null;
      var autoStopped = false;
      var hoverCapable = !!window.matchMedia && window.matchMedia("(hover: hover)").matches;
      var reduceMotion = !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      nodes.forEach(function (n, i) {
        n.setAttribute("role", "tab");
        n.setAttribute("id", "how-step-tab-" + (i + 1));
        n.setAttribute("aria-controls", "how-step-detail");
        n.setAttribute("aria-selected", i === 0 ? "true" : "false");
        n.setAttribute("tabindex", i === 0 ? "0" : "-1");
      });

      function render(i) {
        detailStep.textContent = ("0" + (i + 1)).slice(-2);
        detailTitle.textContent = steps[i].title;
        detailDesc.textContent = steps[i].desc;
        detail.setAttribute("aria-labelledby", nodes[i].id);
        detail.classList.remove("is-in");
        void detail.offsetWidth;
        detail.classList.add("is-in");
      }

      function stopAuto() {
        if (timer) {
          window.clearInterval(timer);
          timer = null;
        }
      }

      function startAuto() {
        if (autoStopped || reduceMotion || timer) return;
        timer = window.setInterval(function () {
          if (selected >= nodes.length - 1) {
            stopAuto();
            return;
          }
          select(selected + 1, false);
        }, 3200);
      }

      function select(i, userAction) {
        if (i < 0 || i >= nodes.length) return;
        selected = i;
        nodes.forEach(function (n, j) {
          n.classList.toggle("is-active", j === i);
          n.classList.toggle("is-done", j < i);
          n.setAttribute("aria-selected", j === i ? "true" : "false");
          n.setAttribute("tabindex", j === i ? "0" : "-1");
        });
        fill.style.width = ((i + 1) / nodes.length * 100) + "%";
        render(i);
        if (userAction) {
          autoStopped = true;
          stopAuto();
        }
        if (rail.scrollWidth > rail.clientWidth + 4) {
          var nr = nodes[i].getBoundingClientRect();
          var rr = rail.getBoundingClientRect();
          var left = rail.scrollLeft + nr.left - rr.left - (rail.clientWidth - nr.width) / 2;
          rail.scrollLeft = Math.max(0, left);
        }
      }

      nodes.forEach(function (n, i) {
        n.addEventListener("click", function () { select(i, true); });
        n.addEventListener("keydown", function (e) {
          var next = -1;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % nodes.length;
          else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + nodes.length) % nodes.length;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = nodes.length - 1;
          if (next >= 0) {
            e.preventDefault();
            select(next, true);
            nodes[next].focus();
          }
        });
        if (hoverCapable) {
          n.addEventListener("pointerenter", function () {
            stopAuto();
            select(i, false);
          });
        }
      });

      shell.addEventListener("pointerleave", function () {
        if (!autoStopped) startAuto();
      });
      shell.addEventListener("focusin", stopAuto);
      shell.addEventListener("focusout", function () {
        if (!autoStopped && !shell.contains(document.activeElement)) startAuto();
      });

      select(0, false);
      shell.classList.add("is-enhanced");

      if ("IntersectionObserver" in window && !reduceMotion) {
        var po = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) {
            if (en.isIntersecting) {
              startAuto();
              po.disconnect();
            }
          });
        }, { threshold: 0.4 });
        po.observe(shell);
      }
    }
  }

})();

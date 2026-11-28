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

  /* ---- 工作方式：五步自动轮播展示台 ---- */
  var shell = document.querySelector("[data-showcase]");
  if (shell) {
    var source = shell.querySelectorAll(".pipeline .node");
    var tabs = shell.querySelector(".showcase__tabs");
    var stage = shell.querySelector(".showcase__stage");
    var copy = shell.querySelector(".showcase__copy");
    var count = shell.querySelector("[data-count]");
    var icon = shell.querySelector("[data-icon]");
    var chip = shell.querySelector("[data-chip]");
    var watermark = shell.querySelector(".showcase__watermark");
    var title = copy ? copy.querySelector("h3") : null;
    var desc = copy ? copy.querySelector("[data-desc]") : null;
    var progress = shell.querySelector(".showcase__progress span");
    if (source.length && tabs && stage && copy && count && icon && chip && watermark && title && desc && progress) {
      var steps = Array.prototype.map.call(source, function (n) {
        var svg = n.querySelector(".node__icon svg");
        var st = n.querySelector(".node__step");
        var h = n.querySelector("h3");
        var p = n.querySelector("p");
        return {
          step: st ? st.textContent.trim() : "",
          title: h ? h.textContent.trim() : "",
          desc: p ? p.textContent.trim() : "",
          svg: svg ? svg.cloneNode(true) : null
        };
      });
      copy.id = "showcase-panel";
      copy.setAttribute("role", "tabpanel");
      var buttons = steps.map(function (s, i) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "showcase__tab";
        b.id = "showcase-tab-" + (i + 1);
        b.setAttribute("role", "tab");
        b.setAttribute("aria-controls", "showcase-panel");
        var num = document.createElement("span");
        num.textContent = s.step;
        b.appendChild(num);
        b.appendChild(document.createTextNode(s.title));
        tabs.appendChild(b);
        return b;
      });
      var selected = 0;
      var timer = null;
      var paused = false;
      var visible = false;
      var reduceMotion = !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      function select(i) {
        if (i < 0 || i >= steps.length) return;
        selected = i;
        var s = steps[i];
        buttons.forEach(function (b, j) {
          b.classList.toggle("is-active", j === i);
          b.setAttribute("aria-selected", j === i ? "true" : "false");
          b.setAttribute("tabindex", j === i ? "0" : "-1");
        });
        copy.setAttribute("aria-labelledby", buttons[i].id);
        count.textContent = s.step;
        watermark.textContent = s.step;
        chip.textContent = s.title;
        title.textContent = s.title;
        desc.textContent = s.desc;
        while (icon.firstChild) icon.removeChild(icon.firstChild);
        if (s.svg) icon.appendChild(s.svg.cloneNode(true));
        progress.style.width = ((i + 1) / steps.length * 100) + "%";
        copy.classList.remove("is-changing");
        void copy.offsetWidth;
        copy.classList.add("is-changing");
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
          select((selected + 1) % steps.length);
        }, 1700);
      }

      buttons.forEach(function (b, i) {
        b.addEventListener("click", function () { select(i); });
        b.addEventListener("keydown", function (e) {
          var next = -1;
          if (e.key === "ArrowRight") next = (i + 1) % steps.length;
          else if (e.key === "ArrowLeft") next = (i - 1 + steps.length) % steps.length;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = steps.length - 1;
          if (next >= 0) {
            e.preventDefault();
            select(next);
            buttons[next].focus();
          }
        });
      });

      shell.addEventListener("pointerenter", function () {
        paused = true;
        stopAuto();
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
      select(0);

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

/* Progressive enhancement only — every page works with this file absent. */
(function () {
  "use strict";

  /* Theme toggle. The initial value is applied inline in <head>. */
  var root = document.documentElement;

  function currentTheme() {
    if (root.dataset.theme) return root.dataset.theme;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("lea-theme", next);
      } catch (e) {
        /* private browsing — the toggle still works for this page */
      }
    });
  });

  /* Copy buttons on code blocks. */
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var code = button.closest("figure").querySelector("code");
      if (!code || !navigator.clipboard) return;
      navigator.clipboard.writeText(code.innerText).then(function () {
        var original = button.textContent;
        button.textContent = "copied";
        setTimeout(function () {
          button.textContent = original;
        }, 1400);
      });
    });
  });

  /* Install tabs on the home page. */
  document.querySelectorAll("[data-tabs]").forEach(function (group) {
    var tabs = Array.prototype.slice.call(group.querySelectorAll("[role=tab]"));
    var panels = Array.prototype.slice.call(group.querySelectorAll("[role=tabpanel]"));

    function select(index) {
      tabs.forEach(function (tab, i) {
        tab.setAttribute("aria-selected", String(i === index));
        tab.tabIndex = i === index ? 0 : -1;
      });
      panels.forEach(function (panel, i) {
        panel.hidden = i !== index;
      });
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () {
        select(i);
      });
      tab.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        var next = (i + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
        select(next);
        tabs[next].focus();
      });
    });
  });

  /* Carousel. Without this the track is still a swipeable, snapping strip —
     this adds arrows, dots, keyboard control and auto-advance. */
  document.querySelectorAll("[data-carousel]").forEach(function (carousel) {
    var track = carousel.querySelector("[data-track]");
    var slides = Array.prototype.slice.call(track.querySelectorAll(".slide"));
    var dots = Array.prototype.slice.call(carousel.querySelectorAll("[data-dot]"));
    if (slides.length < 2) return;

    var index = 0;
    var timer = null;
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var INTERVAL = 7000;

    function paint() {
      dots.forEach(function (dot, i) {
        if (i === index) dot.setAttribute("aria-current", "true");
        else dot.removeAttribute("aria-current");
      });
    }

    function go(next) {
      index = (next + slides.length) % slides.length;
      track.scrollTo({ left: slides[index].offsetLeft, behavior: reduceMotion ? "auto" : "smooth" });
      paint();
    }

    function start() {
      if (reduceMotion || timer) return;
      timer = setInterval(function () {
        if (!document.hidden) go(index + 1);
      }, INTERVAL);
    }

    function stop() {
      clearInterval(timer);
      timer = null;
    }

    /* Advancing on its own is only welcome until the reader engages with it. */
    function stopForGood() {
      stop();
      carousel.removeEventListener("pointerenter", stop);
      carousel.removeEventListener("pointerleave", start);
      carousel.removeEventListener("focusin", stop);
      carousel.removeEventListener("focusout", start);
    }

    carousel.addEventListener("pointerenter", stop);
    carousel.addEventListener("pointerleave", start);
    carousel.addEventListener("focusin", stop);
    carousel.addEventListener("focusout", start);

    carousel.querySelector("[data-prev]").addEventListener("click", function () {
      stopForGood();
      go(index - 1);
    });

    carousel.querySelector("[data-next]").addEventListener("click", function () {
      stopForGood();
      go(index + 1);
    });

    dots.forEach(function (dot, i) {
      dot.addEventListener("click", function () {
        stopForGood();
        go(i);
      });
    });

    track.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      stopForGood();
      go(index + (event.key === "ArrowRight" ? 1 : -1));
    });

    /* A swipe moves the track without going through go() — follow along. */
    var settle = null;
    track.addEventListener(
      "scroll",
      function () {
        clearTimeout(settle);
        settle = setTimeout(function () {
          var nearest = Math.round(track.scrollLeft / (slides[0].offsetWidth + gap()));
          if (nearest !== index && nearest >= 0 && nearest < slides.length) {
            index = nearest;
            paint();
          }
        }, 90);
      },
      { passive: true },
    );

    function gap() {
      return parseFloat(getComputedStyle(track).columnGap) || 0;
    }

    /* Only auto-advance once the carousel is actually on screen. */
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) start();
            else stop();
          });
        },
        { threshold: 0.4 },
      ).observe(carousel);
    } else {
      start();
    }

    paint();
  });

  /* Highlight the table-of-contents entry for the section in view. */
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll(".toc a[href^='#']"));
  if (tocLinks.length && "IntersectionObserver" in window) {
    var byId = {};
    tocLinks.forEach(function (link) {
      byId[decodeURIComponent(link.getAttribute("href").slice(1))] = link;
    });

    var visible = new Set();
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        });
        var first = Object.keys(byId).find(function (id) {
          return visible.has(id);
        });
        if (!first) return;
        tocLinks.forEach(function (link) {
          link.classList.remove("current");
        });
        byId[first].classList.add("current");
      },
      { rootMargin: "-72px 0px -70% 0px" },
    );

    Object.keys(byId).forEach(function (id) {
      var heading = document.getElementById(id);
      if (heading) observer.observe(heading);
    });
  }
})();

// Injected into the recorded page (page.evaluate). Draws the overlay cursor that follows real
// mouse events (white arrow, dark outline, ~22 CSS px, a soft pulse on mousedown), a highlight
// ring for the figure being named, and exposes window.__mw: show/hide/place, pulse, ring/unring,
// an eased smooth scroll and the sticky header height.
// Pointer events never reach the overlay, so hover, tooltips and clicks are the app's own.
(() => {
  if (window.__mw && document.getElementById("__mw_cursor_root")) return;
  const root = document.createElement("div");
  root.id = "__mw_cursor_root";
  root.setAttribute("aria-hidden", "true");
  root.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;contain:strict;";

  const style = document.createElement("style");
  style.textContent = `
    #__mw_cursor_root .mw-arrow{position:absolute;left:0;top:0;width:24px;height:24px;will-change:transform;
      filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.55)) drop-shadow(0 0 1px rgba(0,0,0,.35));transition:opacity .2s ease}
    #__mw_cursor_root .mw-arrow svg{display:block;transform-origin:3px 2px;transition:transform .09s ease-out}
    #__mw_cursor_root .mw-arrow.press svg{transform:scale(.88)}
    #__mw_cursor_root .mw-pulse{position:absolute;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;
      border:2px solid rgba(255,255,255,.9);box-shadow:0 0 0 1px rgba(11,15,20,.35);opacity:0;
      animation:mw-pulse .55s cubic-bezier(.2,.7,.3,1) forwards}
    @keyframes mw-pulse{0%{opacity:.9;transform:scale(.3)}100%{opacity:0;transform:scale(1.35)}}
    #__mw_cursor_root .mw-ring{position:absolute;left:0;top:0;border-radius:7px;opacity:0;
      border:2px solid #f59e0b;box-shadow:0 0 0 1px rgba(11,15,20,.55),0 0 14px rgba(245,158,11,.38);
      transition:opacity .22s ease, transform .22s cubic-bezier(.2,.7,.3,1);transform:scale(1.06)}
    #__mw_cursor_root .mw-ring.on{opacity:1;transform:none}`;
  root.appendChild(style);

  const ring = document.createElement("div");
  ring.className = "mw-ring";
  root.appendChild(ring);

  const arrow = document.createElement("div");
  arrow.className = "mw-arrow";
  arrow.innerHTML =
    '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M3 2 L3 19.4 L7.35 15.35 L10.25 21.7 L13.45 20.25 L10.6 14.05 L16.75 14.05 Z" ' +
    'fill="#ffffff" stroke="#0b0f14" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  root.appendChild(arrow);

  let x = -100;
  let y = -100;
  const place = (nx, ny) => {
    x = nx;
    y = ny;
    arrow.style.transform = `translate(${x - 3}px, ${y - 2}px)`;
  };
  place(x, y);

  const pulse = (px, py) => {
    const p = document.createElement("div");
    p.className = "mw-pulse";
    p.style.left = `${px}px`;
    p.style.top = `${py}px`;
    root.appendChild(p);
    setTimeout(() => p.remove(), 700);
  };

  window.addEventListener("mousemove", (e) => place(e.clientX, e.clientY), { capture: true, passive: true });
  window.addEventListener(
    "mousedown",
    (e) => {
      pulse(e.clientX, e.clientY);
      arrow.classList.add("press");
    },
    { capture: true, passive: true },
  );
  window.addEventListener("mouseup", () => arrow.classList.remove("press"), { capture: true, passive: true });

  (document.body || document.documentElement).appendChild(root);

  // ---- highlight ring: follows its element (by text range or box) every frame while shown
  let ringTarget = null;
  let ringRaf = 0;
  const rectOf = (t) => {
    let r;
    if (t.mode === "text") {
      const range = document.createRange();
      range.selectNodeContents(t.el);
      r = range.getBoundingClientRect();
      if (!r.width) r = t.el.getBoundingClientRect();
    } else r = t.el.getBoundingClientRect();
    return r;
  };
  const follow = () => {
    if (!ringTarget || !ringTarget.el.isConnected) {
      // the element was re-rendered away: never leave a ring around nothing
      ring.classList.remove("on");
      ringRaf = 0;
      return;
    }
    const r = rectOf(ringTarget);
    // pad: one number, or [x, top, bottom] (a big figure's line box is taller than its digits,
    // so a negative top keeps the ring off the label above it)
    const [px, pt, pb] = Array.isArray(ringTarget.pad) ? ringTarget.pad : [ringTarget.pad, ringTarget.pad, ringTarget.pad];
    ring.style.left = `${r.left - px}px`;
    ring.style.top = `${r.top - pt}px`;
    ring.style.width = `${r.width + 2 * px}px`;
    ring.style.height = `${r.height + pt + pb}px`;
    ringRaf = requestAnimationFrame(follow);
  };

  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  window.__mw = {
    show() {
      arrow.style.opacity = "1";
    },
    hide() {
      arrow.style.opacity = "0";
    },
    place,
    pulse,
    pos: () => ({ x, y }),
    /** Ring the element marked data-mw-ring (its text rect when mode is "text"). */
    ring(mode = "text", pad = 5) {
      const el = document.querySelector("[data-mw-ring]");
      if (!el) return false;
      el.removeAttribute("data-mw-ring");
      ringTarget = { el, mode, pad };
      if (!ringRaf) ringRaf = requestAnimationFrame(follow);
      requestAnimationFrame(() => ring.classList.add("on"));
      return true;
    },
    unring() {
      ring.classList.remove("on");
      setTimeout(() => {
        if (!ring.classList.contains("on")) ringTarget = null;
      }, 260);
    },
    header() {
      const h = document.querySelector("header");
      if (!h) return 0;
      const r = h.getBoundingClientRect();
      return r.top <= 1 && getComputedStyle(h).position === "sticky" ? r.height : 0;
    },
    /** Eased scroll to y (clamped), ms long; resolves with the final scrollY. Uses
     *  behavior:"instant" per step because the app sets html{scroll-behavior:smooth}. */
    scrollTo(targetY, ms) {
      return new Promise((resolve) => {
        const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
        const to = Math.max(0, Math.min(max, targetY));
        const from = scrollY;
        const dy = to - from;
        if (Math.abs(dy) < 1) return resolve(scrollY);
        const t0 = performance.now();
        const step = (now) => {
          const t = Math.min(1, (now - t0) / ms);
          window.scrollTo({ top: from + dy * ease(t), behavior: "instant" });
          if (t < 1) requestAnimationFrame(step);
          else resolve(scrollY);
        };
        requestAnimationFrame(step);
      });
    },
  };
})();

// Lets a slide be checked in an ordinary browser: ?preview plays the entrance and reveals the
// narration cues one after another, scaled to the window. The capture never loads with ?preview.
(() => {
  if (!new URLSearchParams(location.search).has("preview")) return;
  const html = document.documentElement;
  html.classList.add("preview", "play");
  const fit = () => {
    const s = Math.min(innerWidth / 1536, innerHeight / 864);
    document.body.style.transform = `scale(${s})`;
  };
  fit();
  addEventListener("resize", fit);
  document.querySelectorAll(".cue").forEach((el, i) => setTimeout(() => el.classList.add("in"), 900 + i * 900));
})();

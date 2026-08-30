/* The background: one violet, five depths of it.

   Five large radial gradients drift on overlapping sine paths and blend
   ADDITIVELY where they meet, which is the whole effect -- 'lighter'
   makes overlapping light add up instead of painting over, and without
   it this is five flat blobs. Ported from design/reference_background.html
   ("Mono"), which is the mode that was picked out of thirteen.

   Deliberately not interactive and not driven by anything. It does not
   read the audit trail, it does not know an order is running, and it
   never reacts to the cursor. A background that responds to things is a
   background people watch, and the surface that deserves watching on
   this site is the agent's terminal.

   A single hue is the reason it can be this large without being a
   problem: the status colours -- amber for waiting on a human, coral for
   refused, green for cleared -- stay the only non-violet things on
   screen, so they still catch the eye immediately.

   No build step, no CDN, no dependency. Plain Canvas 2D. */

(function () {
  "use strict";

  var canvas = document.getElementById("bgCanvas");
  if (!canvas) return;
  var ctx = canvas.getContext("2d");
  if (!ctx) return;                       // no 2D context: leave the ground colour

  var BG = "#0A0714";
  var COLORS = [
    "138,92,255", "108,66,220", "167,139,250", "76,44,168", "199,184,255",
  ];
  var POS = [
    { bx: 0.15, by: 0.22, r: 0.62 },
    { bx: 0.85, by: 0.18, r: 0.58 },
    { bx: 0.50, by: 0.55, r: 0.66 },
    { bx: 0.18, by: 0.82, r: 0.56 },
    { bx: 0.82, by: 0.80, r: 0.60 },
  ];

  /* How bright the field is allowed to get.

     The reference uses 0.16 per layer, which is right for the marketing
     page it was drawn on. These are consoles, and a measured sweep of
     every text element over the field found six -- section ledes and
     footnotes -- landing between 2.6:1 and 4.5:1 against it at 0.16.
     They read fine on the near-black ground they used to sit on.

     This is the same call the CSS aurora already made with
     .aurora-quiet: a working board does not need as much weather as a
     hero. 0.072 is where the last of those six clears AA, measured
     rather than guessed. */
  var ALPHA = 0.072;

  /* Rendered at half resolution and stretched back up by CSS.

     Five full-screen gradient fills per frame is real work, and this has
     to hold frame rate on order.html while the terminal is typing and
     four panels are polling. Quartering the pixels is the cheapest win
     available and it is invisible here, because there is not a hard edge
     anywhere in the image to go soft. */
  var SCALE = 0.5;
  var w = 0, h = 0;

  function resize() {
    w = Math.max(1, Math.round(window.innerWidth * SCALE));
    h = Math.max(1, Math.round(window.innerHeight * SCALE));
    canvas.width = w;
    canvas.height = h;
  }

  function frame(t) {
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";

    for (var i = 0; i < POS.length; i++) {
      var o = POS[i];
      var x = w * o.bx + Math.sin(t * 0.9 + i * 1.4) * 130 * SCALE
                       + Math.cos(t * 1.4 + i) * 70 * SCALE;
      var y = h * o.by + Math.cos(t * 0.7 + i * 2.0) * 110 * SCALE
                       + Math.sin(t * 1.1 + i * 0.8) * 70 * SCALE;
      var r = Math.max(w, h) * o.r;
      var g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, "rgba(" + COLORS[i] + "," + ALPHA + ")");
      g.addColorStop(1, "rgba(" + COLORS[i] + ",0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }

    ctx.globalCompositeOperation = "source-over";
  }

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  var t = 0;
  var running = false;

  function loop() {
    /* Stop while the tab is hidden. requestAnimationFrame does not fire
       there anyway, so this is not what keeps it correct -- it is what
       stops a queued frame spinning up work nobody is looking at, and
       what makes the resume explicit rather than incidental. The last
       frame stays painted, so coming back is seamless. */
    if (document.hidden || reduced.matches) { running = false; return; }
    t += 0.0032;
    frame(t);
    requestAnimationFrame(loop);
  }

  function start() {
    if (running) return;
    running = true;
    requestAnimationFrame(loop);
  }

  /* Reduced motion still gets the background -- it is information about
     where you are, not decoration to be earned. It simply holds still.

     One frame is painted here unconditionally, because resize() clears
     the canvas and leaving the repaint to a future frame means every
     state where a frame does not come next leaves it black: reduced
     motion, a hidden tab, or simply the synchronous moment after a
     resize while the loop waits its turn. Cheaper to always paint than
     to reason about which of those can happen. */
  function render() {
    resize();
    frame(t);
    if (!reduced.matches && !document.hidden) start();
    else running = false;
  }

  window.addEventListener("resize", render);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) render();
  });
  if (reduced.addEventListener) reduced.addEventListener("change", render);

  render();
})();

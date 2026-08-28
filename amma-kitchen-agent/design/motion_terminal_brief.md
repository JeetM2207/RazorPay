# Build task: implement the motion + terminal design system into the real site

## Before you start

1. Save the mockup (`amma_kitchen_direction5_motion.html`) into the repo, e.g.
   `design/reference_mockup.html`, and open it directly as ground truth before
   writing any code.
2. Read `CLAUDE.md` fully, including the earlier chit/paper design pass (now
   being replaced), the no-CDN/no-web-font rule, and the fixes already made
   to `order.html`'s live agent terminal, `merchant.html`'s escalation queue,
   and `dashboard.py`'s audit rendering. This task restyles all of it — same
   functional behavior, new visual system.

## Font decision — resolved, not left open

The mockup loads Inter from Google's CDN. **Self-host it instead**, to keep
the project's existing no-CDN/renders-offline guarantee intact:

- Download Inter's variable `.woff2` file (weights 400–800) and
  JetBrains Mono's `.woff2` (400–600) once, commit them under
  `web/fonts/`.
- Add `@font-face` rules in `shared.css` pointing at the local files, with
  `font-display: swap`.
- Remove the `<link>` tags to `fonts.googleapis.com` — nothing in the final
  site should load from a CDN, matching the project's existing rule.

## Design tokens — add to `shared.css`, replacing the previous chit/paper set

```css
:root{
  --bg:#0A0714;
  --card:#1A1330;
  --card-2:#211A3D;
  --line:rgba(255,255,255,0.09);
  --ink:#F5F3FA;
  --muted:#948FA8;
  --muted-2:#635D78;
  --violet:#8A5CFF;
  --violet-dim:#4A2FA0;
  --lilac:#C7B8FF;
  --amber:#FFB020;
  --coral:#FF7A5C;
  --green:#3DE8A0;
  --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, monospace;
}
```

Reuse the same four-status semantic mapping from earlier design passes —
`--green`/`--violet` for cleared/settled, `--amber` for waiting on a human,
`--coral` for refused/blocked — don't invent new meanings per page.

**Drop the mockup's outer rounded "frame" wrapper.** That existed to present
the mockup like a product screenshot (mirroring the Puzzle reference site).
Your real pages aren't a screenshot inside another screenshot — apply the
aurora background and dark surface directly to the page body, full-bleed, no
extra decorative border-frame around the whole app.

---

## Component-by-component notes

### 1. Aurora background
Port the three drifting blurred blobs as-is (`.aurora`, `.blob-1/2/3`,
`drift1/2/3` keyframes) as a fixed-position layer behind all page content.
**Performance:** large `blur(80px)` regions can be expensive on weaker GPUs —
test actual frame rate on a mid-range laptop, not just your dev machine,
since this needs to run smoothly during a live demo. If it stutters, reduce
blob size or blur radius before reaching for anything more complex.

### 2. Terminal console — must show real data, not canned lines

**This is the part that matters most, so read carefully.** The mockup's
typewriter script types out eight hardcoded lines in a loop — that was
purely for demo purposes. In the real site, wire the typewriter effect to
whatever already drives `order.html`'s live agent terminal (the existing
mechanism that shows negotiation progress today). Each real step — catalog
read, mandate check, cart total, cap comparison, decision, settlement —
should type out as it actually happens, using the real values for that
order, not a fixed script. If the current terminal updates by polling or by
a status callback, keep that mechanism; only change how each update is
*rendered* (typed character-by-character instead of appearing instantly).
When there's no active order, show a single idle line rather than looping
fake activity — don't fabricate agent behavior that isn't happening.

### 3. CountUp — wire to real values

Same principle: the mockup animates from 0 to a hardcoded target. In the
real site, animate from 0 to whatever the actual current value is (today's
revenue, active agent count, the two clear/escalate percentages computed
from real decision-log data). Trigger the count-up once per page load, not
on every re-render, so it doesn't restart distractingly if the page
periodically refreshes data.

### 4. Spotlight cursor-follow effect
Port `.bcard::before` / `.decision::before` and the `spot(e, el)` mousemove
handler as-is onto: the bento feature cards, the merchant's pending-decision
cards, and (new) each row of `dashboard.py`'s rendered audit table if
practical. Keep the effect subtle — it should read as a light physically
in the room, not a following spotlight that draws attention to itself.

### 5. Entrance animations (blur-in headline, staggered log rows)
Keep these, but **only on first paint**, not on every state update — a
merchant whose escalation list re-renders every few seconds shouldn't see
rows blur-in repeatedly. Gate these animations behind a "has this element
already appeared once" check.

### 6. Bento cards — keep the content mapping from the mockup
"Bounded by design" → live hard/soft cap values from the buyer's actual
mandate. "Demand you can see" → real bar heights from the demand/unmatched-
item data already logged. "Every order, gated" → real clear/escalate ratio
computed from the decision log, not the mockup's fixed 94/6. "Full audit,
always" → the buyer's most recent real orders, not the three fixed examples.

---

## Accessibility — non-negotiable

Respect `prefers-reduced-motion`. Wrap every animation in:

```css
@media (prefers-reduced-motion: reduce){
  .blob{ animation:none; }
  .term-line{ /* type instantly instead of character-by-character */ }
  h1 span{ opacity:1; filter:none; animation:none; }
  .log-row{ opacity:1; animation:none; }
}
```

For the terminal specifically, when reduced motion is on, render each line's
full text immediately instead of animating the character-by-character type
effect — the *information* still needs to reach the user, just without the
motion.

---

## Where this lives on the site

- **`web/shared.css`** — all tokens, `@font-face` rules, `.aurora`/`.blob`,
  `.bcard`/spotlight styles, terminal styles, reduced-motion overrides.
- **`web/order.html`** — full treatment: aurora background, blur-in headline,
  the terminal (now wired to real agent state), and the bento grid with real
  data as described above.
- **`web/merchant.html`** — aurora background (can be more subtle/less
  prominent here since this is a working console, not a hero), count-up
  stat cards, spotlight on the decision card, staggered log entrance
  (first-paint only).
- **`web/profile.html` / `web/shop.html`** — token colors and fonts only;
  these are setup forms and don't need aurora/terminal/spotlight treatment.
- **`dashboard.py`** — token colors and fonts in its rendered HTML; spotlight
  on rows only if it doesn't complicate the server-side rendering
  significantly — skip it if it does, this is a nice-to-have here, not core.

## Non-negotiable constraints

- No CDN calls of any kind in the final result — fonts self-hosted per above.
- Do not rename any element `id` or class that existing JavaScript depends
  on (search for `getElementById`/`querySelector` before touching anything
  near the live terminal or escalation handlers).
- Do not touch any `.py` business logic — `dashboard.py` changes are
  rendering/styling only.
- The terminal must never display fabricated agent activity — real data or
  an honest idle state, nothing in between.

## Testing requirements

- Every page loads and functions exactly as before restyling: submit setup
  forms, deploy a real order and watch the terminal reflect real steps,
  accept/decline a real escalation.
- Confirm zero network requests fire for fonts (dev tools network tab).
- Toggle `prefers-reduced-motion` in OS/browser settings and confirm all
  animations stop or simplify per the rules above, with no information lost.
- Check actual frame rate with the aurora background running alongside a
  live order — this is the one part most likely to cause a stutter during a
  demo, so verify it doesn't.
- Confirm the count-up numbers and bento card contents reflect real current
  data, not the mockup's fixed placeholder values, on at least two different
  accounts with different order histories.

## What not to do

- Don't leave the Google Fonts CDN link in place.
- Don't let the terminal or count-up numbers show fake/hardcoded data in the
  shipped site — that was mockup-only scaffolding.
- Don't keep the mockup's outer decorative frame wrapper.
- Don't skip the reduced-motion handling — it's part of the quality bar, not
  optional polish.

## When done

Update `CLAUDE.md`: describe the new design system succinctly, note the
self-hosted font decision and why, and document that the terminal and
count-up elements are now driven by real data rather than the design pass's
placeholder script.

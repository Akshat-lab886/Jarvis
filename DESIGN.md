# JARVIS UI — DESIGN LAW (OPS DECK v5)

> This file is binding. Any UI change MUST pass the audit in §7 before
> it ships. The aesthetic: **amber-phosphor operations console** — a
> purpose-built instrument in the Bloomberg Terminal tradition, not a
> consumer dashboard.

## 1 · Concept

One idea carries everything: **this is an instrument, not an app.**
Density is a trust signal. Empty space is hiding information. The
screen is always live, always dense, always left-aligned.

## 2 · Tokens (locked)

```css
/* surfaces */
--bg-0:#050607  --bg-1:#0B0D0F  --bg-2:#111417  --bg-3:#171B20
--bg-hover:#1E242B
/* lines — shared 1px collapsed borders, never gaps+cards */
--line:#1E252D  --line-strong:#333D48
/* ink */
--text-1:#E8EAED  --text-2:#98A2AD  --text-3:#5A636D
/* signal */
--accent:#FFB000  --accent-dim:rgba(255,176,0,.10)
--accent-line:rgba(255,176,0,.35)
--ok:#3FC874  --warn:#FFB000  --fail:#FF5D5D
/* geometry */
--r:0            /* ZERO radius. No exceptions. */
/* type */
--font-mono:'IBM Plex Mono'  --font-sans:'IBM Plex Sans'
/* motion */
--t:0ms          /* instant cuts. Max 60ms opacity for overlays. */
```

## 3 · Type rules

- **IBM Plex Mono dominates.** All labels, values, timestamps, statuses,
  nav, buttons, meta. If the eye lands on proportional type first, the
  illusion breaks.
- IBM Plex Sans ONLY for chat prose and long descriptions.
- Sizes: 9px micro-labels (letter-spacing .14em, uppercase) · 11px data ·
  12.5px body-meta · 14px chat prose. Nothing larger except 16px status.
- `font-variant-numeric: tabular-nums` on every number.
- Hierarchy via **weight + color intensity**, never size inflation.

## 4 · Layout law: 4-zone collapsed grid

```
status strip (auto)  — full width, 1px bottom border
SYSTEM | TRANSCRIPT | OPERATIONS   (1fr, shared 1px vertical borders)
command line (auto) | log ticker
```
- Panels are **regions divided by lines**, never floating cards.
- No gaps between zones. Borders collapse like an HTML table.
- Transcript is the hero and owns the width. Rails are dense stacks.
- Nothing is centered except nothing. Everything left-aligned.
- Responsive: <1150px hide SYSTEM rail; <760px hide OPERATIONS rail +
  ticker. Never scroll the page; zones scroll internally.

## 5 · Component recipes

| Component | Recipe |
|---|---|
| Status | Text chip `[ OK ]` `[RUN]` `[FAIL]` `[SKIP]` `[PEND]` — mono 10px, colored text, optional 1px border. **NEVER emoji.** |
| Nav | Function codes: `VIS MTG TSK NTS LIB MEM PPL DEV SEC MIS AUT SKL APR` — mono 10px uppercase, active = accent text + accent 2px bottom border. **No icons.** |
| Buttons | `[ LABEL ]` mono 10px, 1px border, hover = accent border+text. Primary = solid accent bg + black text. |
| Rows | 1px bottom hairline separators inside a region; no per-row borders/backgrounds. Hover = `--bg-2` full-bleed. |
| Inputs | `>` prompt prefix, transparent bg, bottom 1px accent border on focus, blinking block cursor `▮` after text. |
| Panels/drawers | Slide-over with instant cut; 1px `--line-strong` border; corner ticks allowed. |
| Empty state | Dense left-aligned row: `NO DATA · <how to create>` in `--text-3`. Never centered hints. |
| Bars | 3px flat fill, no gradient, value right-aligned tabular. |
| Sparkline | 60×18 inline SVG, 1px amber polyline, no fill. |
| Ticker | Single-line dimmed mono log tail, `--text-3`, prefix `LOG│`. |

## 6 · Motion law

- State changes: **instant** (0ms). No fades, slides, bounces.
- Overlays (palette, dialogs): 60ms opacity max.
- Blinking cursor: 1s steps(2) infinite. Pulse dots: 2.4s.
- `prefers-reduced-motion` kills all animation.

## 7 · ANTI-SLOP AUDIT (run before every ship)

1. Zero `border-radius` > 0? ☐
2. Zero emoji in UI chrome? ☐
3. Zero decorative icons (icons only for play/pause/close/trash)? ☐
4. Zero CSS transitions > 60ms (excluding cursor blink)? ☐
5. Zero centered content blocks? ☐
6. Zero cards-in-cards (no bg-on-bg boxes with borders)? ☐
7. Mono type dominates the first glance? ☐
8. Every zone shows real live data (no decorative voids)? ☐
9. All values tabular-nums? ☐
10. Contrast: body text ≥ #98A2AD on #0B0D0F? Accent pairs ≥ 4.5:1? ☐
11. No purple/indigo, no gradients (except none), no glass/blur? ☐
12. Keyboard: ⌘K palette opens, function codes navigate? ☐

**One strike = fix before commit.**

## 8 · Palette law

Amber on near-black is THE identity (Bloomberg's signature). State
colors: green=ok, amber=warn/active, red=fail. Themes swap ONLY the
accent hue (command=phosphor green #37E06E, brutalist=#FF4747,
zen=#B9A6FF, vault=#5AB8FF). Never introduce a second decorative hue.

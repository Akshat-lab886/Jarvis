# JARVIS UI — DESIGN LAW (OPS DECK v5 · PARCHMENT)

> This file is binding. Any UI change MUST pass the audit in §7 before
> it ships. The aesthetic: **brown & white parchment operations
> console** — a paper-ledger instrument in the Bloomberg Terminal
> tradition, not a consumer dashboard.

## 1 · Concept

One idea carries everything: **this is an instrument, not an app.**
Density is a trust signal. Empty space is hiding information. The
screen is always live, always dense, always left-aligned.

## 2 · Tokens (locked)

```css
/* surfaces — PARCHMENT (default): brown & white ops console */
--bg-0:#E8E0D0  --bg-1:#F2ECE0  --bg-2:#FFFFFF  --bg-3:#E3D8C1
--bg-hover:#EFE6D2
/* lines — shared 1px collapsed borders, never gaps+cards */
--line:#3A2C1C  --line-soft:#CDBFA5  --line-strong:#2B1E12
/* ink */
--text-1:#2B1E12  --text-2:#5C4A33  --text-3:#8A7660
/* signal — coffee-brown accent, tan fills; green/red for ok/fail only */
--accent:#6B4A26  --accent-dim:rgba(107,74,38,.10)
--accent-line:rgba(107,74,38,.50)
--ok:#2F7A44  --warn:#8A5A12  --fail:#B3402E
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

Brown & white parchment is THE identity — a paper-ledger console, not
a neon terminal. State colors: green=ok, coffee-brown=warn/active,
oxide-red=fail. There is exactly one theme: parchment. The theme
switcher is deleted; no alternate accent hues ship. Never introduce a
second decorative hue.

## 9 · CINEMATIC LAYER (v6 — HOLO-DECK)

The console earns a cinematic layer BECAUSE it is disciplined. These
are the ONLY ornamental devices allowed, everywhere else stays strict:

1. **HERO GAUGE** — a compact triple-arc dial (CPU/RAM/DISK arcs,
   coffee-brown core) is the transcript's idle centerpiece. It shows
   real live values. It hides once conversation flows (>2 messages).
   Glow permitted HERE only: ≤ `0 0 10px rgba(107,74,38,.18)`.
2. **GRID PAPER** — body background: beige `#E8E0D0` with a faint
   coffee grid (`rgba(107,74,38,.055)`, 28px). Ledger feel, never flat.
3. **SCANLINES** — removed with the dark theme (no overlay on paper).
4. **CORNER BRACKETS** — removed with the dark theme (no HUD chrome
   on paper; desktop keeps none, mobile keeps none).
5. **WAVEFORM** — 24-bar live strip in the transcript header. Bars
   2px, coffee-dim, animated at ~12fps via rAF (cheap). Represents
   audio state; idle = low noise floor.
6. **BIG NUMERALS** — vital values render 22px mono coffee-brown;
   labels 9px. The rail reads as instrumentation, not a table.

Everything in §1–§8 still applies. Glow on anything other than the
hero/core = violation. Gradients other than the vignette = violation.

# JevCodeReview — Blind Review

AI writes 1,000 lines, you shouldn't
have to read 1,000 lines. Instead of "AI code review", this solves the
**attention allocation problem created by generated code**: Jev estimates
which regions of a file deserve human attention, and the editor fades
everything else by real opacity.

## The core algorithm

**1. Split — build a binary tree over the file.**
The file's line range `[1, N]` is recursively bisected until leaves are
≤ 5 lines. Result: a binary tree whose leaves are small code regions.

**2. Map — Jev judges each leaf.**
Every leaf is scored in parallel (32 workers) by one bounded Jev call each:

> *"How valuable is it for a human reviewer to inspect this region in
> order to understand the behavior and correctness of this file?"*

conditioned on the chunk, a compressed file outline, and your **intent**
(why you're reading — e.g. "understand the core function", "find likely
bugs"). Jev returns a 0–4 ordinal, mapped to a probability `p = score/4`.

**3. Reduce — noisy-OR up the tree.**
A region deserves inspection iff *some* line inside it does — a union of
events:

$$P(\text{inspect region}) = 1 - \prod_{c \in \text{children}} (1 - p_c)$$

Key property: `p_parent ≥ p_child`, making every node's score an
**admissible upper bound** on its subtree.

**4. Select — branch-and-bound.**
Best-first expansion from the root: only expand nodes whose bound could
still beat the current k-th-best leaf. With a `budget` cap, the top
regions are found with far fewer Jev calls than scoring every leaf —
provably, because bounds never underestimate.

**5. Contrast — display only.**
Probabilities are stretched in logit space around the file's own baseline
`p₀ = mean(p)`:

$$\operatorname{logit}(p') = \gamma(\operatorname{logit}(p) - \operatorname{logit}(p_0)) + \operatorname{logit}(p_0),\quad \gamma \approx 1.75$$

Monotone ⇒ ordering and bounds unaffected; it only sharpens the visual
separation (less important fades harder, more important pops more).
`p_raw` in the JSON output always shows the un-stretched probability.

**6. Render.**
Bands of `p'` map to opacity:

| probability | opacity |
|---|---|
| < 0.20 | 25% |
| 0.20–0.40 | 40% |
| 0.40–0.60 | 60% |
| 0.60–0.80 | 80% |
| ≥ 0.80 | 100% (sharp) |

VS Code / Emacs fade via decorations / overlays with **real opacity** —
syntax colors are preserved, only salience changes. The region under the
cursor is revealed automatically (hover-to-restore).

In one sentence: **binary-split the file, have Jev independently judge
each leaf, OR the probabilities up the tree, use the monotone tree as an
admissible bound structure for budgeted best-first search, then
contrast-stretch and render as opacity.**

## Components

```
jev_client.py          minimal System One API client (one bounded judgment per chunk)
review.py              split → map → reduce → select → contrast → render
.env                   JEV_KEY
blind-review.el        Emacs front-end: M-x blind-review (fade overlays, reveal at point)
vscode-blind-review/   VS Code extension (same behavior, packaged as .vsix)
IDEA.md                the original design discussion
```

## Terminal usage

```bash
python3 review.py example.py              # hierarchy + faded source view
python3 review.py example.py --top 0.2    # focus top 20% of leaves
python3 review.py example.py --budget 10  # ≤10 Jev calls via branch-and-bound
python3 review.py example.py --goal "find likely bugs"
python3 review.py example.py --contrast 2.5   # aggressive emphasis
python3 review.py example.py --show 26-40     # reveal lines 26-40 in full
python3 review.py example.py --json           # machine-readable (used by editors)
```

## VS Code extension

Install (already packaged as `vscode-blind-review/blind-review-0.1.2.vsix`):

```bash
code --install-extension vscode-blind-review/blind-review-0.1.2.vsix
```

| Key | Command |
|---|---|
| `ctrl+alt+b` | Score & Fade Buffer (prompts for intent) |
| `ctrl+alt+shift+b` | Clear Fading |
| `ctrl+alt+r` | Toggle Reveal All |
| status-bar eye | ranked region list, click to jump |

Settings: `blindreview.scriptPath`, `python`, `workers` (32),
`budget`, `contrast` (1.75), `revealOnCursor`.

Requires `JEV_KEY` in the environment or `~/.config/blind-review/.env`
(the extension injects it into the pipeline; the vsix contains no secrets).

## Emacs usage

```elisp
(add-to-list 'load-path "/path/to/JevCodeReview")
(require 'blind-review)
```

`M-x blind-review` prompts for intent, scores, fades.  `C-c C-r` reveals
the region at point, `C-c C-f` lifts/re-applies all fading, `C-c C-k`
clears.

## Notes

- Jev's score scale is 0–4, sometimes fractional; the map step clamps.
- Scoring is stochastic; the noisy-OR tree + contrast stretch keeps the
  *relative* fade pattern stable across runs.
- Editing after scoring marks the fade stale (VS Code) — re-run to re-score.

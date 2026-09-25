# Blind Review (Jev) — VS Code

Semantic code focus, per IDEA.md: Jev scores every ~5-line leaf of a
binary split of the file — *"how valuable is it for a human reviewer to
inspect this region to understand the behavior and correctness of this
file?"* — and the editor fades everything else with **real opacity**
(syntax colors are preserved; only their salience changes).

## Requirements

- `python3` with `requests`
- `JEV_KEY` in the environment or a `.env` next to `review.py`
- the scoring pipeline: `../review.py` (or set `blindreview.scriptPath`)

## Commands

| Command | Key | What it does |
|---|---|---|
| `Blind Review: Score & Fade Buffer` | `ctrl+alt+b` | prompts for your **intent** (why you're reading — conditions the judgment), runs the pipeline, fades the buffer |
| `Blind Review: Toggle Reveal All` | `ctrl+alt+r` | lift/re-apply all fading |
| `Blind Review: Clear Fading` | `ctrl+alt+shift+b` | remove all fading |

The region under the cursor is **revealed automatically** (IDEA.md's
hover-to-restore). Fading marks itself *stale* when you edit the file —
re-run to re-score.

## Settings

| Setting | Default | |
|---|---|---|
| `blindreview.scriptPath` | bundled `../review.py` | scoring pipeline |
| `blindreview.python` | `python3` | interpreter |
| `blindreview.workers` | `32` | concurrent Jev calls |
| `blindreview.budget` | `null` | branch-and-bound query cap |
| `blindreview.revealOnCursor` | `true` | un-fade region at cursor |

## Install (dev)

```bash
cd vscode-blind-review
npx --yes @vscode/vsce package --allow-missing-repository
code --install-extension blind-review-0.1.0.vsix
```

Or run straight from source: `code --extensionDevelopmentPath=$PWD/vscode-blind-review`

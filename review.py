"""Semantic code focus: map/reduce prototype (see IDEA.md).

Instead of scoring fixed 5-line windows independently, the file is split
recursively in half (binary tree over lines) and priorities are computed
in two phases:

  MAP    : leaves (<= CHUNK lines) are scored by Jev; the ordinal answer
           1..5 is mapped to a probability  p = (s - 1)/4.

  REDUCE : internal nodes aggregate their children with the noisy-OR:

             P(inspect region) = 1 - prod(1 - p_child)

           "a region deserves inspection iff *some* line in it does."
           This guarantees  p_parent >= p_child  for every child, which
           makes node scores admissible upper bounds, so with --budget
           we can run branch-and-bound: expand the most promising node
           first and provably find the top regions without querying Jev
           on every leaf.

Usage:
    python review.py example.py
    python review.py example.py --top 0.3        # focus top 30% of leaves
    python review.py example.py --budget 10      # <=10 Jev calls, best-first
    python review.py example.py --aggregate max  # alternative reduce rule
    python review.py example.py --show 26-40     # full text of one region
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import heapq
import math
import sys
from pathlib import Path

import jev_client

CHUNK = 5  # max lines per leaf (IDEA.md MVP granularity)

# score (probability) -> opacity band (IDEA.md table)
BANDS = [(0.20, 25), (0.40, 40), (0.60, 60), (0.80, 80), (1.01, 100)]

FADE = {25: "\x1b[2;38;5;238m", 40: "\x1b[2;38;5;240m",
        60: "\x1b[2;38;5;244m", 80: "\x1b[2;38;5;248m"}
BOLD, RESET, DIM_WARN = "\x1b[1m", "\x1b[0m", "\x1b[2;33m"


def opacity_for(p: float) -> int:
    for hi, op in BANDS:
        if p < hi:
            return op
    return 100


# ------------------------------------------------------------------ tree

class Node:
    __slots__ = ("start", "end", "depth", "children", "p", "raw",
                 "p_raw", "estimated")

    def __init__(self, start: int, end: int, depth: int = 0):
        self.start, self.end, self.depth = start, end, depth
        self.children: list[Node] = []
        self.p: float | None = None      # probability of "deserves inspection"
        self.raw: int | None = None      # raw Jev 1..5 (leaves / scored nodes)
        self.p_raw: float | None = None  # pre-contrast-stretch p
        self.estimated = False           # p inherited from an ancestor

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def build_tree(nlines: int, lo: int = 1, hi: int | None = None,
               depth: int = 0) -> Node:
    """Binary split of [lo, hi] (1-indexed inclusive) down to <= CHUNK lines."""
    if hi is None:
        hi = nlines
    node = Node(lo, hi, depth)
    if hi - lo + 1 > CHUNK:
        mid = lo + (hi - lo + 1) // 2
        node.children = [build_tree(nlines, lo, mid, depth + 1),
                         build_tree(nlines, mid + 1, hi, depth + 1)]
    return node


def leaves(node: Node) -> list[Node]:
    if node.is_leaf:
        return [node]
    out: list[Node] = []
    for c in node.children:
        out.extend(leaves(c))
    return out


# ------------------------------------------------------------------ state

def first_line(text: str) -> str:
    return text.splitlines()[0][:60] if text.strip() else "(empty)"


def file_summary(lines: list[str]) -> str:
    tree = build_tree(len(lines))
    parts = [f"{s.start}-{s.end}: {first_line(text_of(lines, s))}"
             for s in leaves(tree)]
    return f"{len(lines)} lines. Chunk outline: " + " | ".join(parts)


def text_of(lines: list[str], n: Node, cap: int = 2400) -> str:
    return "\n".join(lines[n.start - 1:n.end])[:cap]


# module-level cache so ask() can reach the lines (kept simple)
lines_cache: list[list[str]] = []


def ask(path: Path, summary: str, n: Node, goal: str) -> float:
    """Query Jev for one region -> p in [0,1].  Raises on failure."""
    state = {
        "file_path": str(path),
        "language": "python",
        "file_summary": summary,
        "review_goal": goal,
        "chunk_start": n.start,
        "chunk_end": n.end,
        "chunk": text_of(lines_cache[0], n),
    }
    v = jev_client.query(state)
    # Jev scores arrive on a 0..4 scale (sometimes fractional)
    n.raw = v["score"]
    return min(max(v["score"], 0.0), 4.0) / 4.0


# ------------------------------------------------------------------ MAP

def map_leaves(path: Path, root: Node, goal: str, workers: int = 32) -> int:
    """Score all leaves in parallel.  Returns number of Jev calls.

    Leaf judgments are independent, so we just saturate the connection
    pool; Jev's own rate limiting is handled by the per-call retry with
    exponential backoff (429s slow individual requests, not the batch)."""
    ls = leaves(root)
    summary = file_summary(lines_cache[0])

    def run(n: Node) -> None:
        try:
            n.p = ask(path, summary, n, goal)
        except Exception as e:
            print(f"[jev] {n.start}-{n.end} failed: {e}", file=sys.stderr)
            n.raw, n.p = 2, 0.5

    with cf.ThreadPoolExecutor(max_workers=min(workers, len(ls))) as ex:
        list(ex.map(run, ls))
    return len(ls)


# ------------------------------------------------- contrast stretch

def sharpen(probs: list[float], gamma: float = 1.0) -> list[float]:
    """Contrast-stretch inspection probabilities in logit space.

    Anchored at the file's own baseline p0 = mean(probs):
        logit(p') = gamma * (logit(p) - logit(p0)) + logit(p0)
    gamma > 1 pushes above-baseline regions toward 1 and below-baseline
    toward 0 (less important fades harder, more important pops), while
    p0 itself is preserved.  Monotone, so ordering (and the
    p_parent >= p_child bound used by branch-and-bound) is unaffected.
    """
    eps = 1e-3
    def lg(p: float) -> float:
        p = min(max(p, eps), 1.0 - eps)
        return math.log(p / (1.0 - p))
    if not probs or gamma == 1.0:
        return list(probs)
    p0 = sum(probs) / len(probs)
    l0 = lg(p0)
    out = []
    for p in probs:
        x = gamma * (lg(p) - l0) + l0
        out.append(min(1.0, max(0.0, 1.0 / (1.0 + math.exp(-x)))))
    return out


# ------------------------------------------------------------------ REDUCE

REDUCE = {
    # union of events: "some child deserves inspection"
    "noisy-or": lambda ps: 1.0 - math.prod(1.0 - p for p in ps),
    # importance of a region = its most important part
    "max": max,
    # average inspection value per line (dilutes large regions)
    "mean": lambda ps: sum(ps) / len(ps),
}


def reduce_tree(root: Node, rule: str = "noisy-or") -> None:
    f = REDUCE[rule]
    def rec(n: Node) -> float:
        if n.is_leaf:
            return n.p
        n.p = f([rec(c) for c in n.children])
        return n.p
    rec(root)


# ------------------------------------------------- branch & bound selection

def top_regions_bestfirst(root: Node, k: int) -> list[Node]:
    """Best-first search using p_parent >= p_child as an admissible bound."""
    heap = [(-root.p, id(root), root)]
    out: list[Node] = []
    while heap and len(out) < k:
        _, _, n = heapq.heappop(heap)
        if n.is_leaf:
            out.append(n)
            continue
        for c in n.children:
            heapq.heappush(heap, (-c.p, id(c), c))
    return out


def budget_explore(path: Path, root: Node, goal: str, budget: int,
                   k: int, workers: int = 8) -> int:
    """Branch-and-bound with Jev in the loop, parallelized by *tiers*:
    each round pops every frontier node that could still beat the
    current k-th best leaf (capped by remaining budget and `workers')
    and scores them concurrently, then pushes all their children.

    Sequential expansion would serialize on one round-trip per node;
    tier expansion turns each round into a single parallel volley.

    Returns the number of Jev queries actually spent (<= budget)."""
    summary = file_summary(lines_cache[0])
    spent = 0

    def kth_best() -> float:
        scored = [l.p for l in leaves(root) if l.p is not None]
        if len(scored) < k:
            return -1.0
        return sorted(scored, reverse=True)[k - 1]

    def query_one(n: Node) -> tuple[Node, float]:
        try:
            return n, ask(path, summary, n, goal)
        except Exception as e:
            print(f"[jev] {n.start}-{n.end} failed: {e}", file=sys.stderr)
            n.raw = 2
            return n, 0.5

    # seed: score the root, then best-first on its halves using the
    # parent's p as the optimistic bound for unscored children
    root.p = ask(path, summary, root, goal); spent += 1
    heap = [(-root.p, id(c), c) for c in root.children]

    while heap and spent < budget:
        kth = kth_best()
        # gather this tier: every frontier node whose admissible bound
        # still beats the current k-th best (skipping prunable ones)
        batch: list[Node] = []
        while heap and len(batch) < min(workers, budget - spent):
            bound = -heap[0][0]
            if bound <= kth:
                heapq.heappop(heap)          # pruned, dead branch
                continue
            batch.append(heapq.heappop(heap)[2])
        if not batch:
            break
        # score the whole tier concurrently
        with cf.ThreadPoolExecutor(max_workers=len(batch)) as ex:
            scored_nodes = list(ex.map(query_one, batch))
        spent += len(batch)
        for n, p in scored_nodes:
            n.p = p
            if not n.is_leaf:
                for c in n.children:
                    heapq.heappush(heap, (-p, id(c), c))  # optimistic bound
    return spent


def propagate_estimates(node: Node, from_p: float | None = None) -> None:
    """Fill unscored leaves with their nearest scored ancestor's p
    (used after budget mode so the whole file still renders)."""
    if node.p is None and from_p is not None:
        node.p, node.estimated = from_p, True
    for c in node.children:
        propagate_estimates(c, node.p if node.p is not None else from_p)


# ------------------------------------------------------------------ render

def render_hierarchy(root: Node, k: int) -> None:
    """Interesting regions: all nodes sorted by aggregate p."""
    nodes: list[Node] = []
    def walk(n: Node) -> None:
        nodes.append(n)
        for c in n.children:
            walk(c)
    walk(root)
    internal = [n for n in nodes if not n.is_leaf]
    internal.sort(key=lambda n: -n.p)
    shown = 0
    print(f"\n{'REGION HIERARCHY (noisy-OR aggregate)':^64}")
    print("─" * 64)
    for n in internal:
        if n.p < 0.25 or shown >= 12:
            continue
        bar = round(n.p * 10)
        est = " ≈" if any(l.estimated for l in leaves(n)) else ""
        print(f"{'  ' * n.depth}{n.start:>4}-{n.end:<4} "
              f"{'█' * bar}{'░' * (10 - bar)}  {n.p:.2f}{est}")
        shown += 1
    print("─" * 64)
    print("(≈ = includes unscored leaves estimated from ancestor; "
          "indent = depth in the binary split)\n")


def render_source(root: Node) -> None:
    lines = lines_cache[0]
    print("SOURCE (dimmed by inspection probability; hover = --show A-B)\n")
    for leaf in leaves(root):
        op = opacity_for(leaf.p)
        body = "\n".join(f"{i:>4}  {lines[i - 1]}"
                         for i in range(leaf.start, leaf.end + 1))
        if op == 100:
            print(f"{BOLD}{body}{RESET}")
        else:
            print(f"{FADE[op]}{body}{RESET}")
            if leaf.estimated:
                print(f"{DIM_WARN}{'':4}  ↑ p estimated from region{RESET}")
    print()


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--top", type=float, default=0.2, metavar="FRAC",
                    help="focus top fraction of leaves (default 0.2)")
    ap.add_argument("--budget", type=int, default=None, metavar="N",
                    help="max Jev queries; branch-and-bound expansion")
    ap.add_argument("--aggregate", choices=list(REDUCE), default="noisy-or")
    ap.add_argument("--goal", default="understand behavioral logic",
                    help="review task T (conditions the judgment)")
    ap.add_argument("--show", default=None, metavar="A-B",
                    help="print lines A-B in full and exit")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable regions JSON (for editors)")
    ap.add_argument("--workers", type=int, default=32,
                    help="concurrent Jev calls (leaves are independent)")
    ap.add_argument("--contrast", type=float, default=1.0, metavar="GAMMA",
                    help="logit-space contrast stretch around the file "
                         "baseline (1.0 = none; ~1.75 makes important "
                         "regions pop harder)")
    args = ap.parse_args()

    lines = args.file.read_text().splitlines()
    lines_cache[:] = [lines]
    root = build_tree(len(lines))
    ls = leaves(root)
    k = max(1, round(len(ls) * args.top))

    if args.show:
        a, b = (int(x) for x in args.show.split("-"))
        print("\n".join(f"{i:>4}  {lines[i - 1]}"
                        for i in range(a, min(b, len(lines)) + 1)))
        return

    if args.budget is not None:
        spent = budget_explore(args.file, root, args.goal,
                               budget=args.budget, k=k, workers=args.workers)
        print(f"[budget] spent {spent}/{len(ls)} possible leaf queries "
              f"via branch-and-bound", file=sys.stderr)
        propagate_estimates(root)
    else:
        n_calls = map_leaves(args.file, root, args.goal, workers=args.workers)
        print(f"[map] scored {n_calls} leaves", file=sys.stderr)

    # contrast stretch: sharpen around the file's own baseline BEFORE
    # reduction (monotone => branch-and-bound bounds stay valid); the
    # un-sharpened probability is kept as p_raw for reference
    if args.contrast != 1.0:
        ls = leaves(root)
        sharpened = sharpen([n.p for n in ls], args.contrast)
        for n, p2 in zip(ls, sharpened):
            n.p_raw, n.p = n.p, p2

    reduce_tree(root, args.aggregate)

    if args.json:
        import json as _json
        print(_json.dumps({
            "file": str(args.file), "goal": args.goal,
            "aggregate": args.aggregate,
            "root_p": round(root.p, 3) if root.p is not None else None,
            "regions": [{"start": n.start, "end": n.end,
                         "score": n.raw, "p": round(n.p, 3),
                         "p_raw": round(n.p_raw, 3) if n.p_raw is not None else None,
                         "estimated": n.estimated}
                        for n in leaves(root)],
        }))
        return

    top = top_regions_bestfirst(root, k)
    render_hierarchy(root, k)
    render_source(root)

    lo, hi = min(n.start for n in top), max(n.end for n in top)
    print(f"focus top {args.top:.0%} → inspect "
          + ", ".join(f"{n.start}-{n.end}" for n in sorted(top, key=lambda x: -x.p))
          + f"  (span {lo}-{hi}, {k}/{len(ls)} leaves)")
    print(f"root p(file needs review) = {root.p:.2f}\n")


if __name__ == "__main__":
    main()
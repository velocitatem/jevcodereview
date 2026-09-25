/**
 * Blind Review (Jev): semantic code focus for VS Code.
 *
 * Scores every ~5-line leaf of a binary split of the file via Jev
 * ("how valuable is inspecting this region to understand this file?"),
 * then fades the buffer with real CSS opacity; syntax colors are
 * preserved, only their salience changes.  The region under the cursor
 * is revealed automatically (the hover equivalent of IDEA.md).
 *
 * Commands:
 *   Blind Review: Score & Fade Buffer        (ctrl+alt+b)
 *   Blind Review: Toggle Reveal All
 *   Blind Review: Clear Fading
 *   Blind Review: Show Review Priority Regions
 */
const vscode = require("vscode");
const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// opacity bands from IDEA.md, keyed by probability
const BANDS = [
  { max: 0.2, opacity: "0.25", label: "25%" },
  { max: 0.4, opacity: "0.4", label: "40%" },
  { max: 0.6, opacity: "0.6", label: "60%" },
  { max: 0.8, opacity: "0.8", label: "80%" },
  { max: 1.01, opacity: "1", label: "sharp" },
];

function bandFor(p) {
  for (const b of BANDS) {
    if (p < b.max) {
      return b;
    }
  }
  return BANDS[BANDS.length - 1];
}

function scriptPath() {
  const cfg = vscode.workspace.getConfiguration("blindreview");
  const configured = cfg.get("scriptPath");
  if (configured) {
    return configured;
  }
  // bundled pipeline (copied into the extension at package time)
  const bundled = path.join(__dirname, "review.py");
  if (fs.existsSync(bundled)) {
    return bundled;
  }
  // dev checkout: pipeline lives one level up
  return path.join(__dirname, "..", "review.py");
}

function envForRun() {
  // JEV_KEY: process.env first, then common .env locations (the packaged
  // extension intentionally contains no secrets)
  const env = { ...process.env };
  if (env.JEV_KEY) {
    return env;
  }
  const candidates = [
    path.join(os.homedir(), ".config", "blind-review", ".env"),
    path.join(__dirname, ".env"),
    path.join(__dirname, "..", ".env"),
  ];
  for (const f of candidates) {
    try {
      const line = fs
        .readFileSync(f, "utf8")
        .split("\n")
        .find((l) => l.startsWith("JEV_KEY="));
      if (line) {
        env.JEV_KEY = line.split("=").slice(1).join("=").trim();
        break;
      }
    } catch (_) {}
  }
  return env;
}

class BlindReviewSession {
  constructor() {
    this.regions = []; // {start,end,p,estimated}
    this.rootP = null;
    this.revealAll = false;
    this.decoTypes = BANDS.filter((b) => parseFloat(b.opacity) < 1).map((b) =>
      vscode.window.createTextEditorDecorationType({
        opacity: b.opacity,
        rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
      })
    );
    this.status = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Right,
      100
    );
    this.status.command = "blindreview.showPriority";
  }

  dispose() {
    this.decoTypes.forEach((d) => d.dispose());
    this.status.dispose();
  }

  cfg(key) {
    return vscode.workspace.getConfiguration("blindreview").get(key);
  }

  regionRange(doc, r) {
    const startLine = doc.lineAt(Math.min(r.start - 1, doc.lineCount - 1));
    const endLine = doc.lineAt(Math.min(r.end - 1, doc.lineCount - 1));
    return new vscode.Range(
      startLine.range.start,
      endLine.range.end
    );
  }

  containsCursor(editor, r) {
    const pos = editor.selection.active;
    const range = this.regionRange(editor.document, r);
    return range.contains(pos);
  }

  /** Apply fade decorations; the region under the cursor is revealed. */
  paint(editor) {
    // reset every band first
    this.decoTypes.forEach((d) => editor.setDecorations(d, []));
    const byBand = new Map();
    for (const r of this.regions) {
      if (this.revealAll) {
        continue;
      }
      if (this.cfg("revealOnCursor") && this.containsCursor(editor, r)) {
        continue; // reveal the region the cursor is in
      }
      const band = bandFor(r.p);
      if (parseFloat(band.opacity) < 1) {
        const deco = {
          range: this.regionRange(editor.document, r),
          hoverMessage:
            (r.estimated ? "≈ p estimated from parent region: " : "") +
            `review priority ${(r.p).toFixed(2)} (opacity ${band.label})`,
        };
        const i = BANDS.indexOf(band);
        if (!byBand.has(i)) {
          byBand.set(i, []);
        }
        byBand.get(i).push(deco);
      }
    }
    this.decoTypes.forEach((d, i) =>
      editor.setDecorations(d, byBand.get(i) || [])
    );
  }

  clear(editor) {
    this.regions = [];
    this.rootP = null;
    this.decoTypes.forEach((d) => editor.setDecorations(d, []));
    this.status.hide();
  }

  setStatus(text) {
    this.status.text = `$(eye) ${text}`;
    this.status.show();
  }
}

function activate(context) {
  const session = new BlindReviewSession();
  context.subscriptions.push(session);

  const scoring = new Set(); // doc URIs currently being scored

  const review = vscode.commands.registerCommand(
    "blindreview.review",
    async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showErrorMessage("Blind Review: no active editor");
        return;
      }
      const doc = editor.document;
      const script = scriptPath();
      if (!fs.existsSync(script)) {
        vscode.window.showErrorMessage(
          `Blind Review: review.py not found (${script}): set "blindreview.scriptPath"`
        );
        return;
      }
      if (scoring.has(doc.uri.toString())) {
        vscode.window.showInformationMessage(
          "Blind Review: already scoring this document"
        );
        return;
      }
      const intents = context.workspaceState.get("intentHistory", []);
      const intent = await vscode.window.showInputBox({
        prompt: "Intent (why are you reading this code?)",
        value: intents[0] || "understand behavioral logic",
      });
      if (intent === undefined) {
        return; // cancelled
      }
      const trimmed = intent.trim() || "understand behavioral logic";
      context.workspaceState.update(
        "intentHistory",
        [trimmed, ...intents.filter((x) => x !== trimmed)].slice(0, 10)
      );

      // snapshot: works on unsaved edits, line numbers map back exactly
      const tmp = path.join(
        os.tmpdir(),
        `blind-review-${Date.now()}${path.extname(doc.fileName) || ".txt"}`
      );
      fs.writeFileSync(tmp, doc.getText());

      const args = [
        script,
        tmp,
        "--json",
        "--goal",
        trimmed,
        "--workers",
        String(session.cfg("workers") ?? 32),
      ];
      const budget = session.cfg("budget");
      if (budget != null) {
        args.push("--budget", String(budget));
      }

      scoring.add(doc.uri.toString());
      session.setStatus("scoring…");
      execFile(
        session.cfg("python") || "python3",
        args,
        { cwd: path.dirname(script), env: envForRun() },
        (err, stdout, stderr) => {
          scoring.delete(doc.uri.toString());
          try {
            fs.unlinkSync(tmp);
          } catch (_) {}
          if (err) {
            session.setStatus("failed");
            vscode.window.showErrorMessage(
              `Blind Review failed: ${stderr.trim().slice(-300) || err.message}`
            );
            return;
          }
          // JSON is the last line of stdout; stderr may interleave in some setups
          const jsonLine = stdout
            .split("\n")
            .reverse()
            .find((l) => l.trim().startsWith("{"));
          let data;
          try {
            data = JSON.parse(jsonLine);
          } catch (e) {
            session.setStatus("failed");
            vscode.window.showErrorMessage(
              `Blind Review: could not parse output: ${stdout.slice(-300)}`
            );
            return;
          }
          session.regions = data.regions;
          session.rootP = data.root_p;
          session.revealAll = false;
          session.paint(editor);
          session.setStatus(
            `blind-review p(root)=${data.root_p.toFixed(2)} · intent: ${trimmed}`
          );
        }
      );
    }
  );

  const clear = vscode.commands.registerCommand("blindreview.clear", () => {
    const editor = vscode.window.activeTextEditor;
    if (editor) {
      session.clear(editor);
    }
  });

  const revealAll = vscode.commands.registerCommand(
    "blindreview.revealAll",
    () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || !session.regions.length) {
        return;
      }
      session.revealAll = !session.revealAll;
      session.paint(editor);
      session.setStatus(
        session.revealAll ? "blind-review: revealed" : "blind-review: faded"
      );
    }
  );

  const showPriority = vscode.commands.registerCommand(
    "blindreview.showPriority",
    async () => {
      if (!session.regions.length) {
        vscode.window.showInformationMessage(
          "Blind Review: nothing scored yet: run Score & Fade Buffer"
        );
        return;
      }
      const editor = vscode.window.activeTextEditor;
      const items = [...session.regions]
        .sort((a, b) => b.p - a.p)
        .map((r) => ({
          label: `${r.estimated ? "≈ " : ""}lines ${r.start}-${r.end}`,
          description: "█".repeat(Math.round(r.p * 10)),
          detail: `p = ${r.p.toFixed(2)}`,
          region: r,
        }));
      const pick = await vscode.window.showQuickPick(items, {
        placeHolder: "Regions by review priority: pick one to jump to it",
      });
      if (pick && editor) {
        const range = session.regionRange(editor.document, pick.region);
        editor.selection = new vscode.Selection(range.start, range.start);
        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      }
    }
  );

  // cursor reveal: repaint when the selection moves (cheap: no scoring)
  const onSelection = vscode.window.onDidChangeTextEditorSelection((e) => {
    if (session.regions.length) {
      session.paint(e.textEditor);
    }
  });

  const onDocChange = vscode.workspace.onDidChangeTextDocument((e) => {
    // buffer changed since scoring: line ranges may drift: fade stale
    if (session.regions.length && e.document === vscode.window.activeTextEditor?.document) {
      session.setStatus("blind-review: stale (re-run to re-score)");
    }
  });

  context.subscriptions.push(
    review,
    clear,
    revealAll,
    showPriority,
    onSelection,
    onDocChange
  );
}

function deactivate() {}

module.exports = { activate, deactivate };

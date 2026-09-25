;;; blind-review.el --- Semantic code focus in Emacs via Jev -*- lexical-binding: t; -*-

;; Blind-review the current buffer: every ~5-line leaf of a binary split
;; of the file is scored by Jev ("how valuable is inspecting this region
;; to understand this file?"), aggregated with noisy-OR up the tree, and
;; the buffer is then dimmed — low-priority regions fade, high-priority
;; regions stay sharp.  This is the editor front-end of IDEA.md.

;; Usage:
;;   M-x blind-review            score + fade current buffer
;;   C-c C-r                     reveal the faded region at point
;;   C-c C-f                     reveal/re-fade everything (toggle)
;;   C-c C-k                     clear all fading
;;   C-u M-x blind-review        re-run with a different review goal

;; Requires: review.py next to this file, JEV_KEY in the environment or
;; a .env beside it, and `python3' + `requests' available.

;;; Code:

(require 'json)

(defgroup blind-review nil
  "Semantic code focus: fade low-review-value code via Jev."
  :group 'programming)

(defcustom blind-review-script
  (expand-file-name
   "review.py"
   (file-name-directory
    (or load-file-name (buffer-file-name) default-directory)))
  "Path to the review.py scoring script."
  :type 'file)

(defcustom blind-review-python "python3"
  "Python interpreter used to run review.py."
  :type 'string)

(defcustom blind-review-goal "understand behavioral logic"
  "Review task T the Jev judgments are conditioned on."
  :type 'string)

(defcustom blind-review-top 0.2
  "Fraction of leaves treated as must-inspect (kept sharp)."
  :type 'number)

(defcustom blind-review-budget nil
  "Max Jev queries (branch-and-bound), or nil to score every leaf."
  :type '(choice (const nil) (natnum)))

(defcustom blind-review-contrast 1.75
  "Logit-space contrast stretch around the file baseline.
1.0 = raw probabilities; larger values make important regions pop
harder and unimportant ones fade harder."
  :type 'number)

(defcustom blind-review-workers 32
  "Concurrent Jev calls; leaf judgments are independent so this can
safely be high (the client retries on rate limits)."
  :type 'natnum)

;; Fading does not change hues: every character keeps its own foreground
;; color, but it is alpha-blended toward the buffer background (alpha 25%
;; .. 100%), which reads as reduced opacity.  Colors are resolved at
;; apply time from the current theme, so light/dark themes both work.

(defun blind-review--alpha-for (p)
  "Map probability P (0..1) to an opacity (bands from IDEA.md)."
  (cond ((< p 0.20) 0.25)
        ((< p 0.40) 0.40)
        ((< p 0.60) 0.60)
        ((< p 0.80) 0.80)
        (t          1.00)))

(defun blind-review--resolved-fg (face)
  "FACE's foreground resolved against the default face, or nil."
  (when (facep face)
    (let ((v (face-attribute face :foreground nil 'default)))
      (and (stringp v)
           (not (member v '("unspecified" "unspecified-fg" "")))
           v))))

(defun blind-review--fg-from-spec (spec)
  "Extract a usable foreground color string from face SPEC, or nil."
  (cond
   ((null spec) nil)
   ((facep spec) (blind-review--resolved-fg spec))
   ((symbolp spec) nil)                       ; e.g. :foreground keywords in a plist
   ((listp spec)
    (car (delq nil (mapcar #'blind-review--fg-from-spec spec))))
   (t nil)))

(defun blind-review--char-fg (pos)
  "Foreground color of the character at POS, or nil if unknown."
  (let ((default (blind-review--resolved-fg 'default))
        (spec (get-char-property pos 'face)))
    (or (blind-review--fg-from-spec spec) default)))

(defun blind-review--color-to-16bit (color)
  "COLOR string -> list of 16-bit RGB values, or nil.
Hex forms (#RGB, #RRGGBB, #RRRRGGGGBBBB) are parsed by hand so the
result is independent of the display's color quantization (batch and
TTY frames clamp `color-values'); named colors fall back to it."
  (when (and (stringp color) (string-prefix-p "#" color))
    (let* ((hex (substring color 1))
           (n (length hex)))
      (cond ((= n 3)
             (mapcar (lambda (d) (* 4369 (string-to-number (char-to-string d) 16)))
                     (append hex nil)))
            ((= n 6)
             (mapcar (lambda (i) (* 257 (string-to-number (substring hex i (+ i 2)) 16)))
                     '(0 2 4)))
            ((= n 12)
             (list (string-to-number (substring hex 0 4) 16)
                   (string-to-number (substring hex 4 8) 16)
                   (string-to-number (substring hex 8 12) 16)))))))

(defun blind-review--blend (fg bg alpha)
  "Blend FG over BG at opacity ALPHA (0..1) -> hex color.
Returns nil if either color is unresolvable (then we keep the text
untouched rather than recolor it blindly)."
  (let ((a (or (blind-review--color-to-16bit fg)
               (ignore-errors (color-values fg))))
        (b (or (blind-review--color-to-16bit bg)
               (ignore-errors (color-values bg)))))
    (when (and a b)
      (apply #'format "#%04x%04x%04x"
             (mapcar (lambda (i)
                       (let ((f (nth i a)) (g (nth i b)))
                         (round (+ (* alpha f) (* (- 1.0 alpha) g)))))
                     '(0 1 2))))))

;; Fallback faces, used only when colors can't be resolved (batch,
;; exotic frames): the previous fixed-greys approach.
(defface blind-review-fallback-1
  '((t :foreground "grey25" :inherit shadow))
  "Heavily faded code (p < 0.20), fallback for color-blend failure.")
(defface blind-review-fallback-2
  '((t :foreground "grey40"))
  "Faded code (p 0.20-0.40), fallback.")
(defface blind-review-fallback-3
  '((t :foreground "grey55"))
  "Half-faded code (p 0.40-0.60), fallback.")
(defface blind-review-fallback-4
  '((t :foreground "grey75"))
  "Slightly faded code (p 0.60-0.80), fallback.")

(defvar-local blind-review--overlays nil
  "Fade overlays currently applied in this buffer.")

(defvar-local blind-review--regions nil
  "Scored regions (alist) from the last blind-review run.")

(defvar-local blind-review--process nil
  "Live scoring process for this buffer.")

(defun blind-review--face-for (p)
  "Fallback fade face for P (used when blending is unavailable)."
  (cond ((< p 0.20) 'blind-review-fallback-1)
        ((< p 0.40) 'blind-review-fallback-2)
        ((< p 0.60) 'blind-review-fallback-3)
        ((< p 0.80) 'blind-review-fallback-4)
        (t          nil)))

;; ------------------------------------------------------------- overlays

(defun blind-review-clear ()
  "Remove all blind-review overlays from the current buffer."
  (interactive)
  (dolist (ov blind-review--overlays)
    (when (overlayp ov) (delete-overlay ov)))
  (setq blind-review--overlays nil)
  (message "blind-review: cleared"))

(defun blind-review--region-bounds (start end)
  "Buffer positions covering lines START..END inclusive."
  (save-excursion
    (save-restriction
      (widen)
      (goto-char (point-min))
      (forward-line (1- start))
      (let ((beg (point)))
        (forward-line (- end start))
        (cons beg (line-end-position))))))

(defun blind-review--fade-region (beg end p)
  "Fade characters in BEG..END to opacity determined by P.
Preserves per-character foregrounds: contiguous characters sharing
one resolved color become one overlay, alpha-blended toward the
background.  Characters whose color can't be resolved fall back to
the fixed fallback face."
  (let* ((alpha (blind-review--alpha-for p))
         (bg (face-attribute 'default :background nil 'default))
         (bg (and (stringp bg) (not (member bg '("unspecified" "unspecified_bg" ""))) bg)))
    (if (and bg (>= alpha 1.0))
        nil                                ; fully opaque: leave text alone
      (let ((pos beg) run-start run-fg)
        (while (< pos end)
          (let ((fg (or (blind-review--char-fg pos)
                        (progn
                          ;; unresolvable: mark the run as fallback
                          'blind-review--unknown-fg))))
            (unless run-start
              (setq run-start pos run-fg fg))
            (unless (equal fg run-fg)
              (blind-review--overlay-run run-start pos run-fg alpha p bg)
              (setq run-start pos run-fg fg)))
          (setq pos (1+ pos)))
        (when run-start
          (blind-review--overlay-run run-start end run-fg alpha p bg))))))

(defun blind-review--overlay-run (beg end fg alpha p bg)
  "Fade one color-run BEG..END (FG at opacity ALPHA over BG)."
  (let ((ov (make-overlay beg end)))
    (cond
     (t
      (let ((color (and bg (blind-review--blend fg bg alpha))))
        (overlay-put ov 'face
                     (if color `(:foreground ,color)
                       (blind-review--face-for p))))))
    (overlay-put ov 'blind-review p)
    (setq blind-review--overlays (cons ov blind-review--overlays))))

(defun blind-review--apply-faces (regions)
  "Create fade overlays for REGIONS (alists with start/end/p/estimated)."
  (save-restriction
    (widen)
    (dolist (r regions)
      (let* ((start (cdr (assq 'start r)))
             (end   (cdr (assq 'end r)))
             (p     (cdr (assq 'p r)))
             (est   (eq t (cdr (assq 'estimated r))))
             (bounds (blind-review--region-bounds start end)))
        ;; est marker lives on a dedicated zero-length overlay so it
        ;; survives the per-run overlays
        (when est
          (let ((mark (make-overlay (car bounds) (car bounds))))
            (overlay-put mark 'before-string
                         (propertize "≈ " 'face '(:foreground "orange")
                                     'help-echo "p estimated from parent region"))
            (overlay-put mark 'blind-review-est t)
            (push mark blind-review--overlays)))
        (blind-review--fade-region (car bounds) (cdr bounds) p)))))

(defun blind-review--apply (regions)
  "Remember REGIONS and fade the buffer with them."
  (blind-review-clear)
  (setq blind-review--regions regions)
  (blind-review--apply-faces regions)
  (blind-review-mode 1)
  (message "blind-review: faded %d regions — C-c C-r reveal, C-c C-f all, C-c C-k clear"
           (length regions)))

(defun blind-review--reveal-at-point ()
  "Delete the fade overlay at point, revealing the code."
  (interactive)
  (let ((ov (car (overlays-at (point)))))
    (cond ((and ov (overlay-get ov 'blind-review))
           (setq blind-review--overlays (delete ov blind-review--overlays))
           (delete-overlay ov)
           (message "blind-review: revealed %s"
                    (if (overlay-get ov 'blind-review-est)
                        "(estimated region)" "region")))
          (t (message "blind-review: nothing faded at point")))))

(defun blind-review-reveal-all ()
  "Temporarily lift all fading, or re-fade if already lifted."
  (interactive)
  (if blind-review--overlays
      (progn
        (dolist (ov blind-review--overlays)
          (when (overlayp ov) (delete-overlay ov)))
        (setq blind-review--overlays nil)
        (message "blind-review: revealed — C-c C-f to fade again"))
    (if blind-review--regions
        (progn
          (blind-review--apply-faces blind-review--regions)
          (message "blind-review: faded again"))
      (message "blind-review: nothing to re-fade — run M-x blind-review first"))))

;; ------------------------------------------------------------- scoring

(defun blind-review--extract-json ()
  "Return the JSON line from the process output buffer, or nil."
  (save-excursion
    (goto-char (point-min))
    (when (re-search-forward "^{.*$" nil t)
      (match-string 0))))

(defun blind-review--report-failure (text)
  (message "blind-review failed: %s"
           (string-trim (substring text 0 (min (length text) 300)))))

(defun blind-review--finish (process)
  "Parse scoring output of finished PROCESS and fade the source buffer."
  (let* ((buf (process-buffer process))
         (origin (process-get process 'origin))
         (tmp (process-get process 'tmp-file))
         (text (if (buffer-live-p buf) (with-current-buffer buf (buffer-string)) "")))
    (unwind-protect
        (let* ((json (and (buffer-live-p buf)
                          (with-current-buffer buf
                            (blind-review--extract-json))))
               (data (and json (ignore-errors (json-read-from-string json))))
               (regions (and data (cdr (assq 'regions data))))
               ;; json.el returns vectors for arrays
               (regions (and regions (append regions nil))))
          (cond (regions
                 (when (and origin (buffer-live-p (marker-buffer origin)))
                   (with-current-buffer (marker-buffer origin)
                     (blind-review--apply regions))))
                (data
                 (blind-review--report-failure
                  (concat "no regions: " (prin1-to-string data))))
                (t
                 (blind-review--report-failure text))))
      (when (and tmp (file-exists-p tmp)) (delete-file tmp))
      (when (buffer-live-p buf) (kill-buffer buf))
      (when (and origin (buffer-live-p (marker-buffer origin)))
        (with-current-buffer (marker-buffer origin)
          (setq blind-review--process nil))))))

(defun blind-review--sentinel (process event)
  "Handle completion of scoring PROCESS (EVENT is the exit message)."
  (when (memq (process-status process) '(exit signal))
    (blind-review--finish process)))

(defun blind-review--run (file-path tmp-file)
  "Spawn review.py asynchronously on FILE-PATH (TMP-FILE for cleanup)."
  (let* ((args (append (list blind-review-python blind-review-script
                             file-path "--json"
                             "--top" (number-to-string blind-review-top)
                             "--goal" blind-review-goal
                             "--workers" (number-to-string blind-review-workers)
                             "--contrast" (number-to-string blind-review-contrast))
                       (when blind-review-budget
                         (list "--budget" (number-to-string blind-review-budget)))))
         (proc (make-process
                :name "blind-review"
                :buffer (generate-new-buffer " *blind-review*")
                :command args
                :connection-type 'pipe
                :sentinel #'blind-review--sentinel)))
    (process-put proc 'origin (copy-marker (point) t))
    (process-put proc 'tmp-file tmp-file)
    (setq blind-review--process proc)
    (message "blind-review: scoring %s (%s)…"
             (file-name-nondirectory file-path) blind-review-goal)))

;;;###autoload
(defun blind-review (goal)
  "Blind-review the current buffer: Jev scores every region and
low-review-value code is faded.  Prompts for the review GOAL (the
intent, e.g. \"understand the core function\" or \"find likely bugs\")
so the judgments are conditioned on why you're reading the code."
  (interactive
   (list (read-string "Intent (why are you reading this code?): "
                      blind-review-goal
                      'blind-review-goal-history)))
  (unless (file-exists-p blind-review-script)
    (user-error "Cannot find %s" blind-review-script))
  (when (string-blank-p goal)
    (setq goal blind-review-goal))
  (setq blind-review-goal goal)
  ;; Score a snapshot of the buffer: works on unsaved edits, and the
  ;; line numbers map straight back onto the buffer.
  (let* ((tmp (make-temp-file "blind-review-")))
    (save-restriction
      (widen)
      (write-region (point-min) (point-max) tmp nil 'silent))
    (blind-review--run tmp tmp)))

;; ------------------------------------------------------------- mode

(defvar blind-review-mode-map
  (let ((m (make-sparse-keymap)))
    (define-key m (kbd "C-c C-r") #'blind-review--reveal-at-point)
    (define-key m (kbd "C-c C-f") #'blind-review-reveal-all)
    (define-key m (kbd "C-c C-k") #'blind-review-clear)
    m))

(define-minor-mode blind-review-mode
  "Keybindings for interacting with blind-review fading."
  :lighter " blind-review"
  :keymap blind-review-mode-map)

(provide 'blind-review)
;;; blind-review.el ends here

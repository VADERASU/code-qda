# Code-QDA

A small tool for coding interview transcripts: open a `.md` file in the browser,
highlight text, and give it a code. It runs on Python's standard library and
saves locally, with no account or package installation needed. To work with
collaborators, host it for them with invite-only accounts; see
[Hosting](#hosting-for-collaborators).

## Get started

```
python3 app.py
```

Open `http://localhost:8000` and choose a transcript. To add your own, put `.md`
files in `md/` and refresh the page. For another port, use `python3 app.py 8001`.
`make run` starts the same app; it never runs transcription.

This local mode has no login, so it only answers on this computer
(`localhost`). It also refuses any request that arrives through a proxy, so
exposing it by mistake fails closed. `make test` runs the app's tests.

## Coding transcripts

- **Reading** in the transcript toolbar adjusts font, text size, line spacing,
  background, and code marks. Transcripts start at 20 px with moderate spacing
  and light annotation fills. Choose underlines or hide the marks for a quieter
  reading view; coded passages remain inspectable. Preferences stay in this
  browser across files and refreshes. Font, size, and spacing also apply to the
  audio verification text. **Reset reading preferences** restores the defaults.
- **Resize Files** by dragging the divider above Codes. Files starts compact
  and scrolls independently, leaving most of the sidebar for Codes.
- **Open a transcript** to resume at its most recently added coding annotation.
  The URL includes `?file=...` so refreshing or bookmarking reopens that transcript;
  browser Back and Forward follow transcript selections.
  Transcripts with no codes start at the beginning.
- **Select any text** to get a popover and type a code name. Click a suggestion,
  or select one with the arrow keys and press Enter or **Apply**. An unambiguous,
  exact name is selected automatically. When no existing codes match, Enter or
  **Apply** creates the typed code. Otherwise, choose **+ Create new code** to
  create it; `Parent / Child` creates nested codes.
- **Drag a code onto another** in the sidebar to nest it. Drop between codes
  to place it at that level, including promoting a subcategory between roots.
  A blue line shows the insertion point. Children take their parent's color;
  promoted codes keep their color, and top-level codes are recolorable.
- **Click a code** in the sidebar to jump to its first instance; hover an
  instance and click **×** to remove it.
- **Edit codes** from either the overview or a transcript: hover a code and
  click **edit** to rename it, or click a root's color block to recolor it.
  **×** deletes a code and its children only when none are used in any transcript.
- **Search codes** at the bottom of the sidebar to filter by name or path,
  including unused codes. Both searches ignore separator differences: `sim to
  real` finds `sim-to-real`, and `sim real` matches its words. Code-name matches
  rank above ancestor matches in suggestions; small spelling mistakes are a
  fallback when there are no literal matches. Press **Esc** to clear the search.
- **Hover coded text** for a compact preview of its codes. **Click a code** in
  the preview to pin the card, reveal that coding's operations, and show its
  annotation instance in the codebook in the same click. Click another code
  to switch its operations and codebook entry. Clicking the transcript
  highlight also pins the card; choose a code when passages overlap.
  Point to a row (or focus its controls) to outline that code's full passage.
  **⋯** offers moving,
  adding a code to the same passage, and removing that coding with **Undo**.
  **Esc**, the close button, or clicking outside closes the card. Keyboard
  users can Tab to a highlight and press Enter or Space to inspect it.
- **Resize a coded span** with **Adjust span** in its annotation card, or **resize** beside
  its sidebar entry. Drag the start or end handle, or select the replacement
  text, then click **Save span**. The code and annotation ID stay the same;
  overlapping annotations are unchanged. **Cancel** or **Esc** discards the
  preview. Focus a handle and use Left/Right arrow keys for precise adjustments.
- **The overview** (with no file open) aggregates code counts across every
  transcript. Click a code to browse all its instances in context, and click a
  card to jump to that passage.
- **+ beside a code's edit button** adds a child beneath that code, at any
  nesting level. The **+** in the Codes heading adds a top-level code.
  Child codes inherit their parent's color. Codes appear even before they
  have annotations. **Prune**, available in the overview, clears out codes
  nothing uses.

Everything saves automatically.

## Reviewing an existing transcript with matching audio

You can use your local copy of a recording to review a shared transcript
**without transcribing it again**. Pull the latest files, then put the recording
in `bescribe/data/audio/` (create the directory if needed). Keep its original
basename: `interview-01.m4a` matches `md/interview-01.tx.json`.

With that saved `.tx.json` transcript present, run from the repository root:

```bash
make prepare AUDIO=bescribe/data/audio/interview-01.m4a
```

Replace the example filename with yours. The command reuses the transcript,
corrections, and review history, and prepares missing playback audio in
`md/audio/` using ffmpeg. No GPU, model installation, or token is needed.
Both audio directories stay out of git.

Start the app with `make run`, open the transcript, and click **Verify
against audio**. The view highlights spoken words and flags likely errors.

1. **Listen:** click **▶** beside a row to play that segment. Click a word to
   start there, or press **Space** to pause or resume.
2. **Correct:** double-click the text or click **edit**. Press **Enter** to
   save or **Esc** to cancel. Click a speaker name to change that segment's speaker.
3. **Confirm:** click **✓** to mark the segment correct and play the next one
   in the selected group.

**Needs review**, **Edited**, and **All** choose which segments **Previous** /
**Next** visit; the full transcript stays visible. **Needs review (0)** means
there are no flagged segments awaiting review. Select **All** to continue
checking the rest. **↑** / **↓** always move to the adjacent transcript row.
Click **Back to coding** to return; saved changes are kept automatically.

Correct these transcripts in the app: it updates the `.tx.json`, regenerates
the `.md`, and adjusts code annotations. Annotations on changed text need review.

[Full controls and details](docs/audio-verification.md).

### If the app says audio is missing

The transcript is available, but its playback audio is missing on this computer.
Text editing still works. Choose the option that matches the file you have:

- **Original recording** (for example, `interview-01.m4a`): run the
  `make prepare AUDIO=...` command above with that recording's path. Keep its
  basename consistent with the saved transcript. With a matching `.tx.json`,
  this only prepares playback audio; you do not need `make install` or a
  Hugging Face token. You do need ffmpeg on your PATH.
- **Prepared playback file** (for example, `interview-01.16k.opus`): copy it
  into `md/audio/` using the exact filename shown in the app. If you started
  the app with `QDA_AUDIO_DIR`, put the file in that directory instead.

Reopen **Verify against audio** after adding the file. Renaming an original
recording to `.opus` does not convert it; use `make prepare` for that.

[Transcribe new recordings](#transcribing-new-recordings).

## Hosting for collaborators

`python3 app.py --hosted` serves the app to the internet through Tailscale
Funnel.

- **Accounts.** Accounts are invite-only. You send each person a one-time
  link, and they choose a password.
- **Projects.** Each study is a separate project, and people see only the
  projects you add them to.
- **Edit history.** Transcript edits record who made them.

[docs/hosting.md](docs/hosting.md) covers setup, administration
(`python3 app.py admin …` or the admin page), and the security model.

## Where the data lives

Transcript data is not in this repository: `md/` is gitignored. A project's
files are:

```
md/
  codes.json                  ← shared codebook
  interview-01.md
  interview-01.tx.json        ← transcript and review history, if present
  interview-01.annot.json     ← saved coding annotations
  audio/                      ← playback audio (IRB-controlled; never in any repo)
```

When hosted, the server's copy is the one everyone edits, so there is nothing
to merge. For version history, make the data directory its own private git
repository and commit from the server only. Moving the data between machines
goes through approved storage.

## Analysis report

[`analysis/report.html`](analysis/report.html) contains findings and interactive
coding charts for the current P1–P5 corpus: `ip1feb102026`,
`ip2feb252026`, `ip3mar062026`, `ip4apr142026`, and
`ip5laptop`. The charts mark transcripts without saved annotations as uncoded.
The overlapping `ip5phone` backup is inventoried without counting P5 twice.
The pilot's annotations are excluded from the primary comparison.
Refresh the report's computed data after coding:

```
python3 analysis/build_report.py
```

This rebuilds the report and `analysis/data/summary.json` without changing
`md/`. New cases are not included automatically. The curated findings in
[`analysis/findings.json`](analysis/findings.json) have exact, timestamped
excerpts checked against the current transcripts; this does not certify audio
or speaker accuracy. The builder validates saved annotation ranges, code IDs,
and quoted evidence. [`analysis/INSIGHTS.md`](analysis/INSIGHTS.md) contains the
readable synthesis and coverage limits. Coding completeness is unconfirmed,
and code counts or presence alone do not establish a research finding.

The one-time transfer of all 330 annotations from `script1–3` is complete.
[Migration details](analysis/migration.md) record the file mapping and audit
trail; the retired scripts remain available in Git history.

## Transcribing new recordings

`make install` selects **MLX Whisper on Apple Silicon Macs** and **WhisperX
on other platforms** (CUDA when available, CPU otherwise). Both produce the
same transcript format. On Macs, word alignment and speaker diarization run
on CPU; transcription uses the Apple GPU through MLX/Metal.

If a recording has no saved transcript yet, follow the
[pipeline setup](bescribe/README.md#setup) (`make install`, model access, and
token). Put recordings in `bescribe/data/audio/`, then run from the repository
root:

```bash
set -a; . bescribe/.env; set +a
make prepare
```

This transcribes new recordings, resumes unfinished work, and reuses finished
transcripts. **`make prepare` runs the entire pipeline:** transcription, a second
transcription for comparison, word timing refinement, transcript installation,
and copying playback audio to the app. Progress messages describe work running
automatically; you do not need to run the individual Python scripts or copy
audio afterward. Wait for **Preparation complete**, then run `make run`.

Keep recording basenames unique and consistent across machines.
`make list env` shows installed environments; `make help` lists commands.
`make list models` lists MLX Whisper sizes, the default, and examples for choosing
one with `MLX_MODEL=... make prepare`.

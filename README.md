# Code-QDA

A browser-based tool for qualitative coding of interview transcripts. Open a
Markdown file, select a passage, and apply a code from a shared codebook. You
can also check a timestamped transcript against its recording. The app runs on
Python's standard library. Use it locally or host it with invite-only accounts.
[Hosting for collaborators](#hosting-for-collaborators)

## Get started

```sh
python3 app.py
```

The app creates an `md/` directory on first start. Put your `.md` transcripts
there, open [http://localhost:8000](http://localhost:8000), and select one
under **Files**. Refresh the page after adding files.

`make run` is another way to start the app. To use port 8001, run
`python3 app.py 8001` or `make run PORT=8001`. Local mode runs without accounts
and accepts requests from this computer only.

## Coding transcripts

- **Reading** in the transcript toolbar adjusts font, text size, line spacing,
  background, and code marks. Transcripts start at 20 px with moderate spacing
  and light annotation fills. Choose underlines or hide the marks for a quieter
  reading view; coded passages remain inspectable. Preferences stay in this
  browser across files and refreshes. Font, size, and spacing also apply to the
  audio verification text. **Reset reading preferences** restores the defaults.
- **Resize Files** by dragging the divider above Codes. Files starts compact
  and scrolls independently, leaving most of the sidebar for Codes.
- **See how far coding has got** in the Files list. A hollow circle means not
  coded, a partly filled one means being coded, and a solid green one means
  mostly coded. Mostly coded means the farthest coded passage is at least 80%
  of the way down. Hover a file for the exact share, its number of codings,
  and its last change. Beside each name are who made the last change and
  when, either a coding or a transcript correction. Codings made before this
  feature, and removals, aren't recorded. The list refreshes after each save
  and when you return to the tab.
- **Open a transcript** to resume at its farthest-down coded passage, which
  is placed at the top of the pane so the uncoded text follows.
  The URL includes `?file=...` so refreshing or bookmarking reopens that transcript;
  browser Back and Forward follow transcript selections.
  Transcripts with no codes start at the beginning.
- **Select any text** to get a popover and type a code name. Click a suggestion,
  or select one with the arrow keys and press Enter or **Apply**. An unambiguous,
  exact name is selected automatically. When no existing codes match, Enter or
  **Apply** creates the typed code. Otherwise, choose **+ Create new code**.
  Use `Parent / Child` to create nested codes.
- **Drag a code onto another** in the sidebar to nest it. Drop between codes
  to place it at that level, including promoting a subcategory between roots.
  A blue line shows the insertion point. Child codes inherit their parent's
  color. Promoted codes keep their color, and you can recolor any top-level code.
- **Click a code** in the sidebar to jump to its first instance. Hover an
  instance and click **×** to remove it.
- **Collapse all codes** beside the Codes heading closes the open branches.
  Click the same button to expand them again.
- **Edit codes** from either the overview or a transcript: hover a code and
  click **edit** to rename it, or click a root's color block to recolor it.
  **×** deletes a code and its children only when none are used in any transcript.
- **Search codes** at the bottom of the sidebar to filter by name or path,
  including unused codes. Both searches ignore separator differences. For
  example, `sim to real` and `sim real` both find `sim-to-real`. Code-name matches
  rank above ancestor matches in suggestions. If there are no literal matches,
  suggestions can match small spelling mistakes. Press **Esc** to clear the search.
- **Hover coded text** for a compact preview of its codes. **Click a code** in
  the preview to pin the card, reveal that coding's operations, and show its
  annotation instance in the codebook in the same click. Click another code
  to switch its operations and codebook entry. Clicking the transcript
  highlight also pins the card; choose a code when passages overlap.
  Point to a row (or focus its controls) to outline that code's full passage.
  Use **⋯** to move or remove a coding or add another code to the same passage.
  Use **Undo** to restore a removed coding.
  **Esc**, the close button, or clicking outside closes the card. Keyboard
  users can Tab to a highlight and press Enter or Space to inspect it.
- **Resize a coded span** with **Adjust span** in its annotation card or
  **resize** beside its sidebar entry. Drag either handle or select the
  replacement text, then click **Save span**. **Cancel** or **Esc** discards
  the preview. Focus a handle and use Left/Right arrow keys for precise
  adjustments.
- **The overview** shows code counts across the project. Open the project URL
  without `?file=...` to see it. Click a code to browse its instances in
  context, then click a card to jump to that passage.
- **+ beside a code's edit button** adds a child beneath that code, at any
  nesting level. The **+** in the Codes heading adds a top-level code.
  Child codes inherit their parent's color. Codes appear even before they
  have annotations. **Prune**, available in the overview, clears out codes
  nothing uses.

Coding changes save automatically. The status below **Search codes** shows
whether the save succeeded.

## Reviewing an existing transcript with matching audio

For local review, put the saved `.tx.json` in `md/` and the matching recording
in `bescribe/data/audio/` (create the directory if needed). Use the same
basename for both files, such as `interview-01.m4a` and
`md/interview-01.tx.json`.

From the repository root, run the command with your recording's path.

```bash
make prepare AUDIO=bescribe/data/audio/interview-01.m4a
```

The command reuses the saved transcript, including its corrections and review
history. It restores any missing Markdown and uses ffmpeg to prepare missing
playback audio in `md/audio/`. This path works without a GPU, model
installation, or Hugging Face token.

Start the app with `make run`, open the transcript, and click **Verify
against audio**. The view highlights spoken words and flags likely errors.

1. **Listen** by clicking **▶** beside a row. Click a word to
   start there, or press **Space** to pause or resume.
2. **Correct text** by double-clicking it or clicking **edit**. Press **Enter** to
   save or **Esc** to cancel. Click a speaker name to change that segment's speaker.
3. **Confirm** with **✓** to mark the segment correct and play the next one
   in the selected group.

**Needs review**, **Edited**, and **All** choose which segments **Previous** /
**Next** visit. The full transcript stays visible. **Needs review (0)** means
there are no flagged segments awaiting review. Select **All** to continue
checking the rest. **↑** / **↓** always move to the adjacent transcript row.
Click **Back to coding** to return to the transcript.

Make corrections in the app so it can update the `.tx.json`, regenerate the
`.md`, and adjust code annotations. Recheck annotations on text you changed.

[Full controls and details](docs/audio-verification.md).

### If the app says audio is missing

You can still edit the transcript. For playback, use the file you have.

- For an **original recording** such as `interview-01.m4a`, run the
  `make prepare AUDIO=...` command above with that recording's path.
- For a **prepared playback file** such as `interview-01.16k.opus`, copy it
  into `md/audio/` using the filename shown in the app. If you started the
  app with `QDA_AUDIO_DIR`, use that directory. You can also use **Upload**
  in the Files list, which works the same way locally and hosted.

Reopen **Verify against audio** after adding the file.

[Transcribe new recordings](#transcribing-new-recordings).

## Hosting for collaborators

`python3 app.py --hosted` serves registered projects and requires sign-in.
The hosting guide walks through configuring the server and publishing the app
over HTTPS with Tailscale Funnel. [Hosting guide](docs/hosting.md)

- **Accounts.** Accounts are invite-only. You send each person a one-time
  link, and they choose a password.
- **Projects.** Each study is a separate project, and people see only the
  projects they've been added to. Anyone signed in can create a project and
  add people who already have accounts.
- **Sandbox.** Everyone has a private sandbox that starts with a made-up,
  partly coded practice interview.
- **Uploads.** Members add transcripts (`.txt`, `.md`, `.tx.json`) and audio
  from the Files list. Uploads never replace an existing file.
- **Transcription.** Under **Recordings**, **Transcribe** runs the
  transcription pipeline on the server's GPU. The result is ready to verify
  against the audio.
  [Setting it up](docs/hosting.md#transcribing-recordings-on-the-server)
- **Edit history.** Transcript corrections and codings record the signed-in
  person's handle and the time.
- **Coding at the same time.** The server merges codebook edits by code.
  If your save conflicts with someone else's annotation changes to the same
  transcript, the app reloads it and asks you to redo your last change.

Manage accounts and projects from the **Admin** page or with
`python3 app.py admin --help`.

### Switch to public access with Funnel

Funnel makes the sign-in page public at the app's existing HTTPS URL. For
IRB-controlled projects, confirm the study protocol permits authenticated remote
access before enabling Funnel. [Hosting guide](docs/hosting.md#what-this-protects-against-and-what-it-doesnt)

Once the host has Funnel permission, run these commands from the account that
runs the code-qda service.

```sh
systemctl --user start code-qda
sudo tailscale funnel --bg --https=443 "unix:/run/user/$(id -u)/code-qda/http.sock"
tailscale funnel status
```

Confirm that Funnel is enabled on port 443. Open the HTTPS URL from a device
off the tailnet and verify that it shows the sign-in page and that signed-out
project API requests return `401`. [Verification checklist](docs/hosting.md#verification-checklist)

### Switch back to tailnet-only access

To close public Funnel access, run these commands on the host from the account
that runs the code-qda service.

```sh
sudo tailscale serve --bg --https=443 "unix:/run/user/$(id -u)/code-qda/http.sock"
tailscale serve status
```

The status should show the app's HTTPS URL followed by `(tailnet only)`.

### Stop sharing code-qda

On the host, remove the HTTPS route on port 443 with the command for the active
mode.

- **Tailnet-only (Serve)** — `sudo tailscale serve --bg --https=443 off`
- **Public (Funnel)** — `sudo tailscale funnel --bg --https=443 off`

Use `tailscale serve status` or `tailscale funnel status` to check the active
routes.

## Where the data lives

Local mode uses the Git-ignored `md/` directory. Hosted projects can use
private storage created by the app or existing directories registered for the
study. [Creating and registering projects](docs/hosting.md#creating-and-registering-projects)

```
md/
  codes.json                  ← shared codebook
  interview-01.md
  interview-01.tx.json        ← transcript and review history, if present
  interview-01.annot.json     ← saved coding annotations
  audio/                      ← default local playback directory
```

In hosted mode, everyone edits the server's copy. For Git history, use a
separate private repository for project data and commit from the server. Move
study data through approved storage and keep recordings and playback audio
out of Git.

## Transcribing new recordings

The pipeline uses **MLX Whisper** on Apple Silicon with native arm64 Python
and **WhisperX** elsewhere (CUDA when available, CPU otherwise). MLX uses the
Apple GPU for transcription and runs alignment and speaker diarization on
the CPU.

For a new recording, the comparison stage needs
`bescribe/pipeline/second_opinion.py` or a cached `.16k.parakeet.json` for that
recording. The script is Git-ignored, so you need to supply it separately
for a fresh run.

Follow the pipeline setup to install the environments and configure model access
and the Hugging Face token. Put recordings in `bescribe/data/audio/` and use a
unique basename for each one. [Pipeline setup](bescribe/README.md#setup)

From the repository root, load the token and prepare the recordings. By default,
`make prepare` processes the `.m4a` files in `bescribe/data/audio/`. Use
`make prepare AUDIO=path/to/recording.m4a` for one file.

```bash
set -a
. bescribe/.env
set +a
make prepare
```

The pipeline transcribes new recordings, compares a second transcription,
refines word timings, and installs the transcript and playback audio. It
reuses completed stages and saved transcripts. When you see
**Preparation complete**, run `make run`.

`make list env` shows installed environments. `make help` lists commands.
`make list models` lists MLX Whisper sizes, the default, and examples for choosing
one with `MLX_MODEL=... make prepare`.

## Tests

- Run `make test` for the standard-library test suite.
- Run `make test-browser` for browser tests with headless Chromium. Set
  `CHROME=/path/to/chromium` if the runner needs an explicit browser path.

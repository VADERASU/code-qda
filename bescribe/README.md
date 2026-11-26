# Robotics Data Practices — Interview Study

Transcription and analysis pipeline for a shadowing study of how robotics
researchers manage experimental data, logs, and tribal knowledge.

**No audio is in this repository, and none may be added.** Transcripts and
their edit logs live in `md/`, which is gitignored; see Data handling.

---

## Data handling

The line is drawn at audio, not at text. This is a private repo, and the
transcripts are the point of it — two people cannot code the same interview if
the transcript only exists on one laptop. Audio is different: one 90-minute
interview is 150 MB as WAV, and git keeps every version of it forever.

| Artefact | Lives where | In git? |
|---|---|---|
| Source audio (`.m4a`, `.wav`, `.opus`) | IRB-approved storage | No |
| Browser render (`md/audio/*.opus`) | Local, or a symlink into that storage | No |
| ASR/alignment JSON, working files | `bescribe/data/` | No |
| Installed `.tx.json` (transcript + edit log) | `md/` | No (own private repo, if any) |
| Installed transcripts (`.md`) and annotations | `md/` | No (own private repo, if any) |
| Speaker maps, pseudonym key | Encrypted, separate from transcripts | No |
| Pipeline code | This repo | Yes |
| Codebook, memos, findings | This repo | Yes |
| Domain vocabulary (`SUSPECT` in `txfmt.py`) | This repo | Yes |

So there are two zones. `bescribe/data/` is gitignored in full — it is the
working area, and everything in it is regenerable from audio. `md/` is the
shared surface: `build_tx.py --install` copies the finished `.tx.json` and
`.md` there. It is no longer committed to this repository, which keeps code and
data apart. Collaborators share it through the hosted app instead (see
[../docs/hosting.md](../docs/hosting.md)). For version history, make `md/` its
own private repository and commit to it from the server only.

To move data between machines, use approved storage — not this repo, not Git
LFS, not a release attachment.

### If something sensitive gets committed

Assume it is permanent until proven otherwise. Deleting the file in a later
commit does not remove it from history. Rewriting history with
`git filter-repo` and force-pushing is the minimum; if the repo was ever
cloned, pulled, or forked in between, treat the exposure as real and report it
per your IRB's incident procedure.

---

## Layout

Repository root, with `bescribe/` holding everything audio-related:

```
.
├── app.py, txfmt.py, static/    # the coding app; see ../README.md
├── md/                          # tracked: transcripts, .tx.json, annotations
│   └── audio/                   # gitignored: browser renders, or symlinks
├── analysis/
│   └── build_report.py          # cross-transcript report -> report.html
└── bescribe/
    ├── pipeline/
    │   ├── transcribe.sh        # audio -> diarized WhisperX JSON + browser audio
    │   ├── transcribe_mlx.py   # Apple GPU ASR -> shared alignment/diarization
    │   ├── backend.py          # platform and device selection
    │   ├── second_opinion.py    # audio -> Parakeet JSON, for disagreement
    │   ├── build_tx.py          # JSON(s) -> .tx.json + .md, installs into md/
    │   └── score_diarization.py # measure a run against hand-marked turns
    ├── analysis/
    │   └── gold_1200_1500.txt   # hand-marked turns, input to score_diarization
    └── data/                    # gitignored in full; you create it
        ├── audio/               # source recordings, from approved storage
        ├── raw/                 # everything the pipeline writes
        └── clean/               # verified transcripts, if you keep them apart
```

The join between this repo and the data is in each `.tx.json`: `build_tx.py`
records the source audio's duration and a SHA-256 of it, so a finding can be
traced to a specific recording without the recording being here.

---

## Setup

From the repository root:

```bash
make install
```

That checks prerequisites (Python 3.10–3.13 and ffmpeg), then builds two
environments — `.venv-wx` for transcription, alignment, and diarization, and
`.venv-asr` for the second opinion. Plain venvs; no conda. `make install-asr`
and `make install-wx` build them separately. Completed installs are reused;
changes to the pipeline install recipe or selected backend update `.venv-wx`.

| Platform | Transcription | Alignment and diarization | Second opinion |
|---|---|---|---|
| Apple Silicon Mac (native arm64 Python) | MLX Whisper on Metal | WhisperX/pyannote on CPU | Parakeet on CPU |
| NVIDIA GPU with CUDA available to PyTorch | WhisperX on CUDA | WhisperX/pyannote on CUDA | Parakeet on CPU |
| Other supported machines | WhisperX on CPU | WhisperX/pyannote on CPU | Parakeet on CPU |

On macOS, install the prerequisites with Homebrew:

```bash
brew install python@3.13 ffmpeg
make install
```

The Makefile looks for Python 3.10–3.13 on `PATH`, checking `python3` and then
`python3.13` through `python3.10`. This handles macOS's system `python3` being
too old or Homebrew's default being too new. To choose an interpreter explicitly,
use `make install PYTHON=python3.13` (or its full path).
Use native arm64 Python on Apple Silicon; an Intel Python running under Rosetta
selects the CPU path.
The MLX install adds `mlx-whisper==0.4.3` alongside `whisperx==3.8.6` in
`.venv-wx`. macOS wheels do not install the Linux CUDA runtime. Model weights
download on first use; MLX's default large-v3 weights alone are about 3 GB.

Backend detection is shared by `make install`, `make prepare`, and the shell
pipeline. Override it consistently when needed:

```bash
make install ASR_BACKEND=mlx       # requires Apple Silicon
make prepare ASR_BACKEND=mlx AUDIO=bescribe/data/audio/interview-01.m4a
# Force the WhisperX CPU path on a Mac instead:
make install ASR_BACKEND=whisperx
TORCH_DEVICE=cpu make prepare ASR_BACKEND=whisperx
```

MLX defaults to `mlx-community/whisper-large-v3-mlx`. Set `MLX_MODEL` to another
MLX Hugging Face model ID or local model directory; `MODEL` selects the model
only for WhisperX. Run `make list models` from the repository root to list sizes,
model IDs, and selection examples; no model installation is needed to view it:

```bash
make list models
MLX_MODEL=mlx-community/whisper-large-v3-turbo make prepare
```

MLX runs ASR in a separate process that exits before
alignment starts, releasing the transcription model's unified memory.
Word timestamps, speaker labels, speaker embeddings, and decoder confidence
are retained in the same JSON format used by the rest of the pipeline.
New MLX transcripts record their backend and model in `.tx.json` provenance.

`TORCH_DEVICE=cpu|cuda|mps` overrides the alignment/diarization device (and
WhisperX ASR's device when that backend is selected). The default `auto` chooses
CUDA when available and CPU otherwise. MPS for the PyTorch stages is experimental;
it is not required for MLX GPU transcription. WhisperX ASR cannot use MPS.
`COMPUTE_TYPE` overrides WhisperX precision; its default uses float16 on CUDA
and float32 on CPU. These settings do not affect MLX precision.

MLX uses Whisper's native transcription windows rather than WhisperX's VAD
batching, so transcripts and speed can differ. Existing reviewed transcripts
are reused across backends; switching backends never triggers a rebuild.
This path has automated integration coverage with model stubs; a full Apple
Silicon audio run is still needed to validate hardware performance and quality.

One step cannot be automated: accept the model licences while logged in to
HuggingFace, then put a token in `bescribe/.env`. `transcribe.sh` reads it from
the environment and never passes it as `--hf_token` — command lines are
world-readable on Linux, so a token in argv is exposed to every user on the
machine for the length of the run. whisperx logs "No --hf_token provided...";
ignore it, huggingface_hub reads `HF_TOKEN` from the environment.

- https://huggingface.co/pyannote/speaker-diarization-community-1
- https://huggingface.co/pyannote/segmentation-3.0
- `cp .env.example .env`, then add `HF_TOKEN`

`make list env` prints the pipeline steps with the interpreter each one needs.

### Why two environments

`whisperx` pins `torch~=2.8.0` — a compatible-release pin, so torch 2.8.x and
nothing else. MLX Whisper is compatible with that pin. The separate ONNX
environment keeps second-opinion dependencies independent. For context, these
are resolved Linux dependency trees, from `pip install --dry-run` (sizes and
CUDA wheels below do not describe a Mac install):

| | whisperx 3.8.6 | nemo_toolkit[asr] 3.0.0 | onnx-asr 0.12 |
|---|---|---|---|
| packages | ~115 | ~170 | 20 |
| torch | **2.8.0** (pinned) | **2.14.0** | none |
| CUDA wheels | cu12 (12.8) | **cu13** | none |
| transformers | 4.57.6 | **5.16.1** | none |
| huggingface_hub | 0.36.2 | **1.29.0** | 1.29.0 |
| also pulls | pyannote, ctranslate2 | lightning, lhotse, hydra, datasets, tensorboard, **wandb**, **sentry-sdk**, opentelemetry | onnxruntime |
| on disk | **7.5 GB** (measured) | several GB | **190 MB** (measured) |

Installing NeMo into the `wx` environment would upgrade torch across the pin
and break whisperx. It also brings a second full CUDA runtime, and pulls
`wandb` and `sentry-sdk` — network clients — into the environment that handles
IRB audio. None of that is needed to get a second transcript.

`onnx-asr` runs the same Parakeet TDT model through ONNX Runtime with no torch
at all, so it can live anywhere. It is the default backend for
`second_opinion.py`. NeMo remains available via `--backend nemo` if you ever
want it; install it in its own environment.

### Run the second opinion at full precision

`onnx-asr` offers `quantization="int8"`, which is 2.7x faster. Do not use it
here. Measured on a 169-second span of ip1: int8 produced 181 words against
full precision's 223, and it repeated Whisper's own error — both heard "common
filter" where the participant said "Kalman filter", which full precision got
right twice. A second opinion that shares the first one's mistakes finds
nothing. Full precision runs at ~4x realtime on a 16-core CPU: about 20 minutes
per 90-minute interview, once.

## Getting the data

A clone gives you the code and the transcripts already produced; it gives you
no audio, and no `data/` directory at all. Before the first run:

```bash
cd bescribe
mkdir -p data/audio ../md/audio
# then copy the recordings in from approved storage:
#   cp /path/to/irb/storage/*.m4a data/audio/
```

Copying is not the only option — `data/audio/` and `md/audio/` can both be
symlinks into approved storage, and the app follows them. Nothing writes to
`data/audio/`, so mounting it read-only is fine.

If you only want to *code* existing transcripts rather than transcribe new
audio, you do not need any of this or `make install`: the transcripts in `md/`
came with the clone, and `make run` from the repo root is the whole setup. You
need audio only for **Verify against audio**, and only for the interviews you
actually want to check.

## Running

The recommended entry point handles the whole batch, skipping recordings that
already have a transcript in `md/` and resuming cached stages for new ones:

```bash
# From the repository root; load the token only when transcribing new audio.
set -a; . bescribe/.env; set +a
make prepare
# Or prepare one recording:
# make prepare AUDIO=bescribe/data/audio/interview-01.m4a
```

This runs transcription, the second opinion, word timing refinement, transcript
installation, and playback audio copying automatically. The output reports
progress; it is not a checklist of commands you need to run. Wait for
**Preparation complete**, then use `make run` to open the app.

It installs text in `md/` and browser audio in `md/audio/` (or `QDA_AUDIO_DIR`).
For custom directories, invoke `python3 bescribe/pipeline/run.py` directly with
the recording paths and `--outdir`, `--install`, or `--audio-dir`.
Existing `.tx.json` files take precedence, including corrections, speaker names,
review status, and edit history. Existing Markdown and annotations are left
untouched; missing Markdown is recovered from the saved transcript. A broken
transcript causes an error rather than an automatic rebuild. No GPU, model
environment, or token is needed for reuse; missing playback audio only needs
ffmpeg. Keep the same, unique recording basenames on every collaborator's
machine: `interview.m4a` maps to `interview.tx.json`.

Transcript, Markdown, and annotation filenames use the recording basename.
The `.16k` suffix identifies normalized audio and raw processing intermediates;
it stays out of newly built transcript filenames. Older transcript names are
still recognized and reused, including their corrections and review history.

Pull before processing recordings, and commit the `.tx.json`, `.md`, and any
`.annot.json` together after transcription or verification. Collaborators can
produce different transcripts on different GPUs and share them through git.
The ignored `data/raw/` cache does not need to be shared.

The individual stages below are for manual pipeline development. Skip these
commands when using `make prepare`; it already runs them for you.

```bash
cd bescribe && set -a && . ./.env && set +a

# 1. transcribe + diarize (automatically selects MLX on Apple Silicon)
./pipeline/transcribe.sh data/audio/*.m4a

# 2. second opinion (needed for realignment) ~20 min per interview, CPU
../.venv-asr/bin/python pipeline/second_opinion.py data/raw/*.16k.wav

# 3. realign, then build transcripts and install into md/
../.venv-wx/bin/python pipeline/realign.py data/raw/*.16k.json
python3 pipeline/build_tx.py data/raw/*.16k.json --install ../md/

# 4. put the browser audio where the app looks for it
cp data/raw/*.16k.opus ../md/audio/

# 5. code and verify
cd .. && make run
```

Every step writes to and reads from `data/raw/` — that is `transcribe.sh`'s
default `OUTDIR`. If you override `OUTDIR`, change it in steps 2-4 as well.

Step 4 is a separate step because `--install` deliberately copies only text:
the `.tx.json` and the `.md`, which are tracked. Audio never goes into git, so
`build_tx.py` will not put it in `md/`. Symlink instead of copying, or point
the app elsewhere entirely, and skip step 4:

```bash
QDA_AUDIO_DIR=bescribe/data/raw python3 app.py
```

In step 3, glob `*.16k.json` and not `*.json`. The broader pattern also matches
the `.speakers.json` maps the same script writes, and building a transcript
from a speaker map produces a nonsense `.speakers.speakers.json`.

`transcribe.sh` takes `ASR_BACKEND`, `MLX_MODEL`, `TORCH_DEVICE`, `COMPUTE_TYPE`,
`WHISPERX` (path to the binary), `WX_PYTHON` (pipeline interpreter), `PYTHON`
(stdlib interpreter), `MODEL`, `SPEAKERS`, `DIARIZE_MODEL`, `OUTDIR`,
`INSTALL_DIR`, and `PROMPT` from the environment. Model executables default to
the repository's `.venv-wx`, independent of the current working directory.
`INSTALL_DIR` defaults to this checkout's `md/`; the second-opinion and alignment
scripts accept the equivalent `--install-dir`. Each stage reuses existing
results before loading models. For deliberate experiments, use a separate
output/install directory. Individual Python stages have `--force`, and
`transcribe.sh` accepts `FORCE=1`, to explicitly rebuild their results.
**`build_tx.py --force` discards corrections and verification in the files it
replaces**; normal collaborative runs must not use it. `build_tx.py` is
stdlib only and runs on the system Python.

### Speakers

Step 3 writes a `<name>.speakers.json` stub the first time it sees a file,
mapping each diarized cluster to itself:

```json
{ "SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01" }
```

Listen to 30 seconds of each and replace the values with real names before
building the final transcript. If you already built it, the normal build
reuses it; editing the stub alone changes nothing. You can assign speakers in
the verification UI while preserving reviewed work. Only for a transcript
with no corrections, verification, or coding to preserve, rebuild with
`build_tx.py --force --install ../md/` and the original input JSON.

Speaker count is per interview, not global: `transcribe.sh` defaults to 3 and
special-cases `ip4*` to 4. Add a case there for a recording with a different
number of voices — running with the wrong count silently merges two people into
one label, and nothing downstream can detect it.

## The `.tx.json` format

`build_tx.py` writes `<name>.tx.json` — the source of truth for a transcript —
and renders `<name>.md` from it. Only the `.md` is coded against; only the
`.tx.json` is edited. It holds:

- **segments** with start/end and per-word timings, so the UI can seek to a
  word. `orig` keeps what the ASR actually said; `text` is the current version.
- **speaker evidence**: `diarization.turns` retains the exact original intervals;
  segment `speaker_basis` retains the assignment's original timestamps and label;
  word `speaker` retains the machine cluster ID. Stable segment IDs survive timing
  reordering. These fields are optional for older transcripts.
- **`speaker_suggestion`**: a review-only proposal after timing repair. Building
  new transcripts and running `apply_timings.py` refresh suggestions from saved
  intervals. The app offers accept/keep controls and protects prior decisions.
- **an append-only `edits` log**: every correction with its timestamp, author,
  and the text before and after.
- **`risk` and `flags`** per segment: the review queue, computed by `txfmt.flag`.

The edit log is the durable artefact. It is the audit trail an IRB protocol
wants, and it is also the corrections corpus — `(audio span, ASR hypothesis,
human truth)` triples that feed keyterm biasing now and can feed a fine-tune
later. Transcripts are regenerable from audio; that log is not.

Both ASR backends use `pipeline/diarize_transcript.py` to retain the intervals
from the same run that assigned the speaker labels. The WhisperX CLI writes its
ASR/alignment intermediate under `data/raw/.asr/`; the completed raw JSON is only
published after diarization succeeds. CUDA remains the default when available.
This prevents interrupted diarization from leaving an apparently completed
transcript without speaker evidence. No extra diarization run is needed.

## Verification

Correct transcripts in the app: `python3 app.py`, open an interview, click
**Verify against audio**. Click a word to hear it, loop a segment until you can
make it out, correct it in place. The server re-renders the `.md` and moves
code annotations onto it in the same step.

The queue is the worst ~15% of each recording, ranked by three signals:

| Signal | What it catches |
|---|---|
| Disagreement with the second ASR model | the strongest predictor; needs `second_opinion.py` |
| Whisper's segment `avg_logprob`, ranked *within the file* | genuinely uncertain decoding |
| Near-identical neighbouring segments | Whisper repetition loops, which are fluent and so invisible to confidence |
| `SUSPECT` patterns in `txfmt.py` | known mishearings that are also real English |

Ranking is within-file on purpose: median segment logprob across these six
recordings ranges from -0.11 to -0.22, so any absolute cutoff floods the worst
recording and skips the best.

### How disagreement is measured

The two word streams are aligned **once per file, on word order**, and each
segment is charged for the words the other model did not produce. It is not a
per-segment comparison over a time window, and the difference is not academic:
when the models genuinely disagree over a stretch, one of them falls seconds
behind the other, so a time-windowed comparison flags every *correct* segment
after the error until they resync. Measured on ip1, one real disagreement at
57 s produced three flags, two of them on correct text. Sequence alignment has
no such failure mode.

A segment is flagged when the other model missed more than 40% of its words,
**or** missed two or more content words — the second catches a one-word
substitution in a long sentence, which barely moves a rate.

What this cannot catch is a *correlated* error. At 57:36 of ip1 both models
hear "common filter" where the participant said "Kalman filter"; they agree, so
disagreement is silent. That segment is still queued, on confidence — which is
why the other signals stay.

**Do not flag on WhisperX's per-word `score`.** It is a wav2vec2 forced-alignment
posterior, not a confidence: short function words score near zero while being
perfectly correct. Flagging on it selected 37% of one interview and ranked a
correct sentence first, because it contained "a" and "it". The UI still *shows*
low-scoring words dimmed — it just never queues on them.

Add new mishearings to `SUSPECT` in `txfmt.py` as you find them. Nothing is
substituted automatically any more: a suspect term is raised for you to judge
with the audio in front of you, which is what the old auto-substituting
`GLOSSARY` got wrong when a loose pattern turned "whole" into "Waymole"
throughout a transcript.

## The decoder prompt — we don't use one

`transcribe.sh` passes no `--initial_prompt`. That is whisperx's shipped
default (`"initial_prompt": None`), and we measured our way back to it.

`--initial_prompt` is not a description of the audio. It fills Whisper's
`<|startofprev|>` slot, which in training held **the transcript of the
preceding audio** — whisperx passes it as `previous_tokens` to
`faster_whisper.get_prompt`, which emits
`<|startofprev|> {prompt} <|startoftranscript|> <|en|>`. With the default that
slot is omitted entirely. It is also re-applied to **every batch**, not just
the first window, and `condition_on_previous_text` is `False`, so whatever is
in it colours the whole transcript.

### The experiment

Three arms on two full interviews, one model load, identical VAD, decoding
confirmed byte-identical on a repeat run — so every difference is the prompt.

| | A: meta-description + term list | B: transcript-style prose | C: no prompt |
|---|---|---|---|
| ip1 "rosbag" | 17 | 24 | 0 |
| ip5 "LiDAR" | 3 | 10 | 1 |
| ip1 collapsed spans / words lost | 7 / 172 | 11 / 274 | — |
| ip5 collapsed spans / words lost | 14 / 401 | 24 / 774 | — |
| ip5 hallucinated sign-offs | 2 | 0 | 0 |
| unmatched vs Parakeet | 0.125 | 0.136 | 0.125 |

Prompting genuinely helps rare terms — "rosbag" appears 0 times without it.
But both prompts make Whisper **drop real speech** and emit a short fluent
sentence over the top. "Collapsed spans" counts VAD spans where a prompted arm
produced 15+ fewer words than the unprompted one for the same audio.

The decisive part is what replaces the speech. It inherits the prompt's
register, so the more in-distribution the prompt, the better the fabrication
hides:

| prompt | what appeared in place of real speech |
|---|---|
| A | `"Thank you for watching."` — obviously wrong |
| B | `"How accurate is the depth estimate?"` — replacing 49 real words |
| B | `"How would you let them handle that?"` — replacing 65 real words |
| B | `"Do you have an overhead camera on the robot arm?"` — replacing 57 |

For qualitative analysis that trade is backwards. A mangled term is
recoverable: `SUSPECT` flags it, the second model disagrees about it, and you
fix it against the audio. A fabricated interviewer question that reads
perfectly is not recoverable, because nothing tells you to look — and it would
be quoted in a paper as something a participant was asked.

So: no prompt, and the vocabulary is recovered afterwards where a human
decides. `PROMPT=...` re-enables it for a single run if you ever want to
compare.

### Caveat

Two interviews, one model, no hand-transcribed ground truth. "Unmatched vs
Parakeet" is a proxy and it penalises arm B for correctly hearing terms that
Parakeet — which takes no prompt — gets wrong. The content-collapse counts do
not depend on that proxy; they are a direct comparison of how much speech each
arm transcribed for the same audio.

**The transcripts in `data/raw/` were produced with prompt A** and carry this
damage. Re-running them under the default is about 40 seconds of GPU each for
the ASR pass.

---

## Comparing pipeline configurations

`score_diarization.py` measures a run against speaker turns you mark by ear —
three minutes is enough — so VAD thresholds, normalisation, and diarization
models can be compared on numbers rather than impressions. The gold format and
the lenient/strict distinction are documented in the script's header;
`analysis/gold_1200_1500.txt` is a worked example.

```bash
python3 pipeline/score_diarization.py \
  analysis/gold_1200_1500.txt data/raw/gold3_*.json
```

It is stdlib only, like `build_tx.py`.

### Trial: speaker assignment after timing repair

`pipeline/trial_speakers.py` compares speaker assignments at the original raw
timestamps and the current `.tx.json` timestamps using the **same** diarization
intervals. It writes a separate JSON report and an HTML listening sample;
it never updates transcripts, annotations, or edit history. Existing speaker
edits are protected and used only as imperfect consistency evidence. An
unchanged label is not treated as correct.

Historical raw JSON files do not contain the original diarization intervals.
`pipeline/capture_diarization.py` can capture a fresh run with a pinned model
revision, including both regular and exclusive intervals. Use `.venv-wx/bin/python`
for capture and system Python for comparison. Capture automatically selects CUDA
when available; use `--device cuda` to require it or `--device cpu` for CPU runs.
The process must have access to the GPU; a restricted sandbox may hide a working
host GPU. Fresh runs, especially on excerpts,
can cluster voices differently; the report includes agreement with historical
machine labels at the original timestamps to expose that limitation. Cluster
IDs are matched using original machine word labels, never the human edits.

See [the trial report](../analysis/diarization-trial.md) for results, exact
commands, and the listening samples. No trial suggestions are applied by the
normal preparation pipeline.

## Reproducibility notes

- Model versions and WhisperX flags are pinned in `transcribe.sh`. Change them
  in a commit, not ad hoc, so transcripts are traceable to a configuration.
- Record parameter changes in the commit message that makes them — including
  changes that produced no visible difference in output. That null result is
  the expensive one to rediscover.
- Transcripts are regenerable from audio; verified corrections are not. The
  edit log inside each `.tx.json` is the artefact to protect, and it is in git
  precisely so that it is not on one laptop.

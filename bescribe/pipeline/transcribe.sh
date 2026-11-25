#!/usr/bin/env bash
# transcribe.sh — audio -> diarized, timestamped, word-confidence JSON
#
# One-time setup, from the repository root:
#   make install-wx                # platform-selected ASR + alignment/diarization
#   # Accept the model licenses on HuggingFace while logged in:
#   #   huggingface.co/pyannote/speaker-diarization-community-1
#   #   huggingface.co/pyannote/segmentation-3.0
#   cp bescribe/.env.example bescribe/.env    # then add HF_TOKEN
#
# Usage, from bescribe/:
#   set -a && . ./.env && set +a
#   WHISPERX=../.venv-wx/bin/whisperx ./transcribe.sh data/audio/interview01.m4a
#   WHISPERX=../.venv-wx/bin/whisperx ./transcribe.sh data/audio/*.m4a   # batch

set -euo pipefail

# Everything downstream — second_opinion.py, build_tx.py, the paths in
# README.md and `make list env` — reads from data/raw. Changing this default without
# changing those leaves step 2 globbing an empty directory.
OUTDIR="${OUTDIR:-./data/raw}"
MODEL="${MODEL:-large-v3}"
SPEAKERS="${SPEAKERS:-3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$SCRIPT_DIR/../../md}"
FORCE="${FORCE:-0}"
PYTHON="${PYTHON:-python3}"
ASR_BACKEND="$("$PYTHON" "$SCRIPT_DIR/backend.py" --backend "${ASR_BACKEND:-auto}")"
WX_PYTHON="${WX_PYTHON:-$SCRIPT_DIR/../../.venv-wx/bin/python}"
TORCH_DEVICE="${TORCH_DEVICE:-auto}"

# Speaker count is a property of the interview, not a global default. Forcing
# the wrong number silently merges two people into one label, which corrupts
# attribution in a way nothing downstream can detect — ip4 has four (both
# interviewers, the participant, and a fourth), and running it at 3 collapsed
# two of them. Override per file here.
speakers_for() {
  case "$1" in
    ip4*) echo 4 ;;
    *)    echo "$SPEAKERS" ;;
  esac
}

# Verified present in whisperx 3.8.6: --diarize_model and --speaker_embeddings.
# community-1 is a clear improvement on the 3.1 default. Accept its licence on
# HuggingFace first, or diarization will fail at load time.
DIARIZE_MODEL="${DIARIZE_MODEL:-pyannote/speaker-diarization-community-1}"

# whisperx lives in its own environment (it pins torch~=2.8.0 and cannot share
# one with incompatible second-opinion dependencies). Defaults point to the
# repo's environment; set WHISPERX or WX_PYTHON to use another installation.
WHISPERX="${WHISPERX:-$SCRIPT_DIR/../../.venv-wx/bin/whisperx}"

# ---------------------------------------------------------------------------
# DECODER PROMPT — off by default, which is what whisperx ships.
#
# --initial_prompt does not describe the audio to Whisper. It fills the
# <|startofprev|> slot, which in training held *the transcript of the preceding
# audio*. whisperx passes it as `previous_tokens` to faster_whisper.get_prompt,
# which emits <|startofprev|> {tokens} <|startoftranscript|> <|en|>. With the
# default (None) that slot is omitted entirely.
#
# We A/B'd three arms on two full interviews — same model, same VAD, decoding
# verified byte-identical on a repeat run, so every difference is the prompt:
#
#   A  old meta-description + term list
#   B  transcript-style prose using the same terms in sentences
#   C  no prompt (the shipped default)
#
# Rare terms are the case FOR prompting, and it is real:
#
#   ip1  "rosbag"  A: 17   B: 24   C:  0
#   ip5  "LiDAR"   A:  3   B: 10   C:  1
#
# But both prompts make Whisper drop real speech and emit a short fluent
# sentence in its place. Counting VAD spans where a prompted arm produced 15+
# fewer words than the unprompted one:
#
#            collapsed spans / words lost
#   ip1      A:  7 / 172     B: 11 / 274
#   ip5      A: 14 / 401     B: 24 / 774
#
# And what replaces the speech gets worse as the prompt gets more in-
# distribution, because the fabrication inherits the prompt's register:
#
#   A  "Thank you for watching."                     — obviously wrong
#   B  "How accurate is the depth estimate?"         — reads as a real question
#   B  "How would you let them handle that?"         — replacing 65 real words
#
# For qualitative analysis that trade is backwards. A mangled term is
# recoverable: SUSPECT in txfmt.py flags it, the second model disagrees about
# it, and you fix it against the audio. A fabricated interviewer question that
# reads perfectly is not recoverable, because nothing tells you to look.
#
# So: no prompt. Recover the vocabulary afterwards, where a human decides.
# Set PROMPT to re-enable it for a specific run; it is passed only if non-empty.
# ---------------------------------------------------------------------------
PROMPT="${PROMPT:-}"

# Check the inputs before loading a multi-GB model. An unmatched glob is
# passed through literally (nullglob is off), so a missing or empty data/audio/
# would otherwise arrive as a file named "*.m4a" and fail deep inside ffmpeg
# with an error that says nothing about the real problem.
if [ "$#" -eq 0 ]; then
  echo "usage: $0 data/audio/*.m4a" >&2
  exit 1
fi
for SRC in "$@"; do
  if [ ! -f "$SRC" ]; then
    echo "no such audio file: $SRC" >&2
    echo "Interview audio is not in this repo. Copy it from approved storage" >&2
    echo "into bescribe/data/audio/ first:  mkdir -p data/audio" >&2
    exit 1
  fi
done

mkdir -p "$OUTDIR"

for SRC in "$@"; do
  BASE="$(basename "${SRC%.*}")"
  WAV="$OUTDIR/${BASE}.16k.wav"
  JSON="$OUTDIR/${BASE}.16k.json"
  CACHED=""
  if [ "$FORCE" != 1 ]; then
    for DIR in "$INSTALL_DIR" "$OUTDIR"; do
      for STEM in "$BASE" "${BASE}.16k"; do
        for SUFFIX in .tx.json .realigned.tx.json; do
          if [ -f "$DIR/$STEM$SUFFIX" ]; then
            CACHED="$DIR/$STEM$SUFFIX"
            break 3
          fi
        done
      done
    done
    if [ -z "$CACHED" ] && [ -f "$JSON" ]; then
      CACHED="$JSON"
    fi
  fi

  echo "=== $BASE ==="
  if [ -n "$CACHED" ]; then
    echo "  reusing $CACHED"
    # A resumed pipeline may have the raw JSON but still need a WAV for the
    # second opinion and alignment. Shared finished transcripts do not.
    if [ "$CACHED" = "$JSON" ] && [ ! -f "$WAV" ]; then
      ffmpeg -hide_banner -loglevel error -i "$SRC" \
             -ac 1 -ar 16000 -c:a pcm_s16le "$WAV"
    fi
  else
    if [ "$ASR_BACKEND" = mlx ]; then
      "$PYTHON" "$SCRIPT_DIR/backend.py" --backend mlx --check >/dev/null
      if ! command -v "$WX_PYTHON" >/dev/null 2>&1; then
        echo "Pipeline Python not found: $WX_PYTHON; run make install-wx" >&2
        exit 1
      fi
    elif ! command -v "$WHISPERX" >/dev/null 2>&1; then
      echo "whisperx not found: $WHISPERX" >&2
      echo "Run 'make install-wx' from the repo root, then:" >&2
      echo "  WHISPERX=../.venv-wx/bin/whisperx $0 $*" >&2
      exit 1
    fi

    if ! command -v "$WX_PYTHON" >/dev/null 2>&1; then
      echo "Pipeline Python not found: $WX_PYTHON; run make install-wx" >&2
      exit 1
    fi

    # The token is passed through the environment, never as --hf_token. Command
    # lines are world-readable on Linux: anyone with an account on this machine can
    # read a running process's argv out of `ps`, so a token there is exposed for
    # the whole run. The environment is only readable by the same user and root.
    #
    # whisperx logs "No --hf_token provided..." — ignore it. huggingface_hub picks
    # HF_TOKEN up from the environment and the gated pyannote models load fine;
    # verified against community-1.
    if [ -z "${HF_TOKEN:-}" ]; then
      echo "HF_TOKEN is not set." >&2
      echo "  cd bescribe && set -a && . ./.env && set +a" >&2
      echo "(and accept the model licences at huggingface.co/pyannote/" >&2
      echo " speaker-diarization-community-1 and .../segmentation-3.0)" >&2
      exit 1
    fi

    # Normalise only when ASR needs a WAV. Playback can use the source directly.
    if [ ! -f "$WAV" ]; then
      echo "  [1/3] normalising audio..."
      ffmpeg -hide_banner -loglevel error -i "$SRC" \
             -ac 1 -ar 16000 -c:a pcm_s16le "$WAV"
    fi

    echo "  [2/3] transcribing + diarizing..."
    # An `x && y` one-liner here is a trap: it returns non-zero when PROMPT is
    # empty, which is the normal case, and this script runs under `set -e`.
    NSPK="$(speakers_for "$BASE")"

    PROMPT_ARG=()
    if [ -n "$PROMPT" ]; then
      PROMPT_ARG=(--initial_prompt "$PROMPT")
    fi

    if [ "$ASR_BACKEND" = mlx ]; then
      FORCE_ARG=()
      if [ "$FORCE" = 1 ]; then FORCE_ARG=(--force); fi
      "$WX_PYTHON" "$SCRIPT_DIR/transcribe_mlx.py" "$WAV" \
        --output "$JSON" --speakers "$NSPK" \
        --diarize-model "$DIARIZE_MODEL" --device "$TORCH_DEVICE" \
        --install-dir "$INSTALL_DIR" --prompt "$PROMPT" ${FORCE_ARG[@]+"${FORCE_ARG[@]}"}
    else
      DEVICE_ARG=()
      if [ "$TORCH_DEVICE" != auto ]; then DEVICE_ARG=(--device "$TORCH_DEVICE"); fi
      if [ "$TORCH_DEVICE" = mps ]; then
        echo "WhisperX ASR does not support mps; use ASR_BACKEND=mlx on Apple Silicon" >&2
        exit 1
      fi
      # The guarded expansions support empty arrays under macOS Bash 3.2's
      # nounset behavior while preserving spaces in individual arguments.
      mkdir -p "$OUTDIR/.asr"
      "$WHISPERX" "$WAV" \
        --model "$MODEL" \
        --language en \
        --compute_type "${COMPUTE_TYPE:-default}" \
        --vad_onset 0.35 \
        --vad_offset 0.25 \
        --batch_size 8 \
        --output_format json \
        --output_dir "$OUTDIR/.asr" \
        ${PROMPT_ARG[@]+"${PROMPT_ARG[@]}"} ${DEVICE_ARG[@]+"${DEVICE_ARG[@]}"}
      # Only publish the final raw JSON after diarization succeeds. Keeping ASR
      # output separate prevents an interrupted run from caching missing labels.
      "$WX_PYTHON" "$SCRIPT_DIR/diarize_transcript.py" "$OUTDIR/.asr/${BASE}.16k.json" \
        --audio "$WAV" --output "$JSON" --speakers "$NSPK" \
        --model "$DIARIZE_MODEL" --device "$TORCH_DEVICE"
    fi
  fi

  # Browser-playable render for the verification UI. The 16 kHz WAV is 150 MB
  # for a 90-minute interview — far too big to seek around in a browser. Opus
  # at 32 kbps is ~10 MB and indistinguishable for speech. ASR always reads the
  # WAV; only the UI reads this.
  OPUS="$OUTDIR/${BASE}.16k.opus"
  if [ ! -f "$OPUS" ]; then
    echo "  [3/3] rendering browser audio..."
    ffmpeg -hide_banner -loglevel error -i "$SRC" \
           -ac 1 -ar 16000 -c:a libopus -b:a 32k -application voip "$OPUS"
  fi

  echo "  -> $OUTDIR/${BASE}.16k.json"
done

if [ "${QDA_PIPELINE_MANAGED:-0}" != 1 ]; then
  echo
  echo "This script only handles transcription and playback conversion."
  echo "To finish the pipeline automatically, run from the repository root:"
  echo "  make prepare AUDIO=/path/to/original-recording.m4a"
  echo "Use your recording's path. Completed stages are reused."
fi

# code-qda
#
# The app itself has no dependencies — `make run` needs nothing but Python 3.
# `make install` builds the two environments the *transcription pipeline*
# needs. Apple Silicon uses MLX for ASR; other platforms use WhisperX.
# WhisperX handles alignment/diarization on both. See bescribe/README.md.

# Prefer a supported interpreter, even when macOS's older python3 is first
# on PATH. Keep explicit PYTHON overrides and fall back for app-only use.
ifeq ($(origin PYTHON), undefined)
PYTHON := $(shell for python in python3 python3.13 python3.12 python3.11 python3.10; do \
	"$$python" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info < (3, 14)))' >/dev/null 2>&1 \
	&& { printf '%s\n' "$$python"; exit; }; \
	done; printf '%s\n' python3)
endif
PORT   ?= 8000
AUDIO  ?= bescribe/data/audio/*.m4a
ASR_BACKEND ?= auto
SELECTED_BACKEND := $(shell $(PYTHON) bescribe/pipeline/backend.py --backend "$(ASR_BACKEND)")
export ASR_BACKEND PYTHON

VENV_WX  := .venv-wx
VENV_ASR := .venv-asr
WX_PY    := $(VENV_WX)/bin/python
ASR_PY   := $(VENV_ASR)/bin/python

# Marker files, not the venv directories: a directory's mtime changes whenever
# anything inside it does, so make would consider the target stale forever.
WX_STAMP  := $(VENV_WX)/.installed-$(SELECTED_BACKEND)
ASR_STAMP := $(VENV_ASR)/.installed

.PHONY: run prepare install install-asr install-wx check list clean clean-venvs help

## run: serve the app on port 8000 (PORT=... to change; no dependencies)
run:
	@bash -c 'set -m; $(PYTHON) app.py $(PORT) & fg %1'

## prepare: prepare AUDIO=... (default: all .m4a recordings); reuse saved transcripts, transcribe new ones
prepare:
	$(PYTHON) bescribe/pipeline/run.py $(AUDIO)

## install: build pipeline environments (MLX on Apple Silicon; WhisperX elsewhere)
install: install-asr install-wx
	@echo
	@echo "Both environments are ready."
	@echo
	@echo "Remaining manual step — accept the pyannote model licences while"
	@echo "logged in to HuggingFace, then put the token in bescribe/.env:"
	@echo "  https://huggingface.co/pyannote/speaker-diarization-community-1"
	@echo "  https://huggingface.co/pyannote/segmentation-3.0"
	@echo "  https://huggingface.co/settings/tokens"
	@echo
	@test -f bescribe/.env \
	  && echo "  bescribe/.env exists." \
	  || echo "  cp bescribe/.env.example bescribe/.env   # then add HF_TOKEN"
	@echo
	@$(MAKE) --no-print-directory list env

## install-asr: second-opinion ASR only (~190 MB, no torch, no CUDA)
install-asr: $(ASR_STAMP)

$(ASR_STAMP): | check
	@echo "==> $(VENV_ASR): onnx-asr (Parakeet TDT via ONNX Runtime)"
	@$(PYTHON) -m venv $(VENV_ASR)
	@. $(VENV_ASR)/bin/activate && python -m pip install --quiet --upgrade pip
	@. $(VENV_ASR)/bin/activate && python -m pip install --quiet "onnx-asr[cpu,hub]"
	@. $(VENV_ASR)/bin/activate && python -c "import onnx_asr, onnxruntime; \
	  print('    onnx-asr', onnx_asr.__version__, '| onnxruntime', onnxruntime.__version__)"
	@touch $@

## install-wx: selected ASR backend + WhisperX alignment and diarization
install-wx: $(WX_STAMP)

$(WX_STAMP): Makefile bescribe/pipeline/backend.py | check
	@echo "==> $(VENV_WX): $(SELECTED_BACKEND) ASR + WhisperX alignment + pyannote"
	@$(PYTHON) -m venv $(VENV_WX)
	@. $(VENV_WX)/bin/activate && python -m pip install --quiet --upgrade pip
	@. $(VENV_WX)/bin/activate && python -m pip install "whisperx==3.8.6" $(if $(filter mlx,$(SELECTED_BACKEND)),"mlx-whisper==0.4.3",)
	@. $(VENV_WX)/bin/activate && python -c "import whisperx, torch; \
	  print('    whisperx ok | torch', torch.__version__, \
	        '| cuda', torch.cuda.is_available())"
	@if [ "$(SELECTED_BACKEND)" = mlx ]; then \
	  . $(VENV_WX)/bin/activate && python -c "import mlx.core as mx, mlx_whisper; assert mx.metal.is_available(), 'MLX Metal GPU unavailable'; print('    mlx-whisper ok | Metal available')"; \
	fi
	@touch $@

## check: verify the things the pipeline needs but cannot install
check:
	@echo "==> checking prerequisites"
	@command -v $(PYTHON) >/dev/null \
	  || { echo "    MISSING: $(PYTHON)"; exit 1; }
	@$(PYTHON) -c 'import sys; \
	  sys.exit(0) if (3, 10) <= sys.version_info < (3, 14) else sys.exit(1)' \
	  || { echo "    Python 3.10–3.13 required, found $$($(PYTHON) -V)"; \
	       echo "    macOS: brew install python@3.13"; \
	       echo "    Then retry: make install PYTHON=python3.13"; \
	       exit 1; }
	@echo "    $$($(PYTHON) -V)"
	@$(PYTHON) bescribe/pipeline/backend.py --backend "$(ASR_BACKEND)" --check
	@$(PYTHON) -c 'import venv' 2>/dev/null \
	  && echo "    venv ok" \
	  || { echo "    MISSING: the venv module — a separate package on Debian/Ubuntu."; \
	       echo "             apt install python3-venv"; \
	       exit 1; }
	@command -v ffmpeg >/dev/null \
	  && echo "    ffmpeg $$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)" \
	  || { echo "    MISSING: ffmpeg — required for every pipeline step."; \
	       echo "             brew install ffmpeg (macOS) / apt install ffmpeg (Linux)"; \
	       exit 1; }
	@if [ "$(SELECTED_BACKEND)" = mlx ]; then \
	  echo "    Apple Silicon: MLX/Metal transcription; CPU alignment and diarization"; \
	elif gpu_info=$$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null); then \
	  echo "    GPU: $$gpu_info"; \
	else echo "    No NVIDIA GPU found. WhisperX will use CPU (slower)."; fi

list:
	@$(if $(filter env models,$(MAKECMDGOALS)),:,echo "Usage: make list env | make list models")

# Make treats each word as a goal. Expose the listing topics only with `list`.
ifneq ($(filter list,$(MAKECMDGOALS)),)
.PHONY: env models

## list env: show installed environments and next steps
env:
	@echo "Environments"
	@echo "  ASR backend: $(SELECTED_BACKEND) (ASR_BACKEND=auto|mlx|whisperx)"
	@echo "  MLX model choices and usage: make list models"
	@test -f $(WX_STAMP)  && echo "  [x] $(VENV_WX)"  || echo "  [ ] $(VENV_WX)   (make install-wx)"
	@test -f $(ASR_STAMP) && echo "  [x] $(VENV_ASR)" || echo "  [ ] $(VENV_ASR)  (make install-asr)"
	@echo
	@echo "Audio is not in the repo. Before the first run:"
	@echo "  mkdir -p bescribe/data/audio md/audio"
	@echo "  cp /path/from/approved/storage/*.m4a bescribe/data/audio/"
	@echo
	@echo "Recommended: reuse shared transcripts, process only new recordings:"
	@echo "  set -a; . bescribe/.env; set +a   # token only needed for new audio"
	@echo "  make prepare   # or: make prepare AUDIO=bescribe/data/audio/interview-01.m4a"
	@echo "  make run       # open a transcript, then Verify against audio"
	@echo
	@echo "Pipeline details: bescribe/README.md"

## list models: list MLX Whisper models and selection examples (no model installation needed)
models:
	@echo "MLX Whisper models (Apple Silicon)"
	@echo "  Size             Parameters  MLX_MODEL"
	@echo "  tiny                  39M    mlx-community/whisper-tiny-mlx"
	@echo "  base                  74M    mlx-community/whisper-base-mlx"
	@echo "  small                244M    mlx-community/whisper-small-mlx"
	@echo "  medium               769M    mlx-community/whisper-medium-mlx"
	@echo "  large-v3-turbo       809M    mlx-community/whisper-large-v3-turbo"
	@echo "  large-v3            1550M    mlx-community/whisper-large-v3-mlx (default)"
	@echo
	@echo "Smaller models use less memory; Turbo favors speed over full large-v3."
	@echo "Choose explicitly for new recordings:"
	@echo "  MLX_MODEL=mlx-community/whisper-large-v3-turbo make prepare"
	@echo "  MLX_MODEL=mlx-community/whisper-small-mlx make prepare AUDIO=bescribe/data/audio/interview-01.m4a"
	@echo
	@echo "MLX_MODEL also accepts a compatible Hugging Face model ID or local model directory."
	@echo "Existing transcripts are reused when you change models."
	@echo "For the WhisperX backend, use MODEL=small (or another Whisper model name)."
endif

## clean-venvs: delete both environments
clean-venvs:
	rm -rf $(VENV_WX) $(VENV_ASR)

clean: clean-venvs

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'

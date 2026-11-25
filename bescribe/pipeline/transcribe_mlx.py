#!/usr/bin/env python3
"""MLX Whisper ASR, followed by WhisperX alignment and pyannote diarization."""
import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from artifacts import DEFAULT_INSTALL_DIR, find_transcript, write_json
from diarize_transcript import diarize_result
from backend import apple_silicon, clear_cache, torch_device


DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"


def transcribe(audio, model, prompt):
    import mlx_whisper
    # Alignment supplies word timestamps. Do not spend memory computing
    # Whisper's own word timestamps or carry previous text into later windows.
    return mlx_whisper.transcribe(
        str(audio), path_or_hf_repo=model, language="en", task="transcribe",
        word_timestamps=False, condition_on_previous_text=False,
        initial_prompt=prompt or None, verbose=False,
    )


def align_and_diarize(raw, audio_path, device, speakers, diarize_model):
    import whisperx

    if not raw["segments"]:
        return {"segments": [], "word_segments": [], "language": "en"}
    audio = whisperx.load_audio(str(audio_path))
    print(f"  aligning words on {device}...", flush=True)
    model, metadata = whisperx.load_align_model(language_code="en", device=device)
    # WhisperX 3.8.6 preserves avg_logprob when splitting segments, so the
    # verification queue retains MLX's decoder confidence after alignment.
    result = whisperx.align(raw["segments"], model, metadata, audio, device,
                            return_char_alignments=False)
    del model
    gc.collect()
    clear_cache(device)

    print(f"  diarizing on {device}...", flush=True)
    result = diarize_result(result, audio, device, speakers, diarize_model)
    result["language"] = "en"
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--model", default=os.environ.get("MLX_MODEL", DEFAULT_MODEL))
    ap.add_argument("--speakers", type=int, default=3)
    ap.add_argument("--diarize-model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"),
                    default=os.environ.get("TORCH_DEVICE", "auto"))
    ap.add_argument("--prompt", default="")
    ap.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--asr-only", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.asr_only and not args.force:
        cached = find_transcript(args.audio.stem, args.install_dir, args.output.parent)
        if cached or args.output.exists():
            print(f"  reusing {cached or args.output}")
            return
    if not apple_silicon():
        ap.error("MLX requires native arm64 Python on an Apple Silicon Mac")
    if not args.audio.is_file():
        ap.error(f"no such audio file: {args.audio}")
    if args.speakers < 1:
        ap.error("--speakers must be positive")
    if args.asr_only:
        write_json(args.output, transcribe(args.audio, args.model, args.prompt))
        return
    if not os.environ.get("HF_TOKEN"):
        ap.error("HF_TOKEN is required for pyannote; load bescribe/.env first")

    # Exit the MLX worker before loading PyTorch models to release all of the
    # ASR model's unified memory, including mlx-whisper's global model cache.
    with tempfile.TemporaryDirectory(prefix="bescribe-mlx-") as directory:
        raw_path = Path(directory) / "asr.json"
        print(f"  transcribing with MLX ({args.model})...", flush=True)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), str(args.audio),
                        "--asr-only", "--output", str(raw_path), "--model", args.model,
                        "--prompt", args.prompt], check=True)
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    result = align_and_diarize(raw, args.audio, torch_device(args.device),
                               args.speakers, args.diarize_model)
    result["models"] = {"asr": f"mlx-whisper {args.model}", "align": "wav2vec2",
                        "diarize": args.diarize_model}
    write_json(args.output, result)


if __name__ == "__main__":
    main()

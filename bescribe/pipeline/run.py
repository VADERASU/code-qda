#!/usr/bin/env python3
"""Process new recordings and reuse transcripts shared through git."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from artifacts import DEFAULT_INSTALL_DIR, ROOT, find_transcript
from build_tx import reuse_transcript


PIPELINE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", nargs="+", type=Path)
    ap.add_argument("--outdir", type=Path, default=ROOT / "bescribe/data/raw")
    ap.add_argument("--install", type=Path, default=DEFAULT_INSTALL_DIR)
    ap.add_argument("--audio-dir", type=Path,
                    help="browser audio destination (default: QDA_AUDIO_DIR or INSTALL/audio)")
    args = ap.parse_args()
    args.outdir = args.outdir.resolve()
    args.install = args.install.resolve()
    audio_dir = args.audio_dir or Path(os.environ.get("QDA_AUDIO_DIR", args.install / "audio"))

    # Validate the batch before doing expensive work. A basename is the shared
    # recording identity, so two different sources must not silently collide.
    sources = {}
    for audio in args.audio:
        # The supplied basename is the shared identity, even when the file
        # itself is a symlink to differently named approved storage.
        audio = audio.absolute()
        if not audio.is_file():
            ap.error(f"no such audio file: {audio}")
        if audio.stem in sources and sources[audio.stem] != audio:
            ap.error(f"recordings must have unique basenames: {audio.stem}")
        sources[audio.stem] = audio

    env = os.environ.copy()
    env.update(OUTDIR=str(args.outdir), INSTALL_DIR=str(args.install), FORCE="0",
               QDA_PIPELINE_MANAGED="1")
    env.setdefault("WHISPERX", str(ROOT / ".venv-wx/bin/whisperx"))
    env.setdefault("WX_PYTHON", str(ROOT / ".venv-wx/bin/python"))
    env.setdefault("PYTHON", sys.executable)
    print("Preparing recordings automatically. All remaining processing and playback setup run here.",
          flush=True)
    for base, audio in sources.items():
        print(f"\nPreparing {audio.name}", flush=True)
        src = args.outdir / (base + ".16k.json")
        # Check shared work first, including incomplete/invalid artifacts,
        # before any model or local audio processing can run.
        reused = reuse_transcript(src, args.install)
        browser_audio = audio_dir / (base + ".16k.opus")
        if not reused or not browser_audio.exists():
            print("  Preparing playback audio..." if reused
                  else "  Transcribing and preparing playback audio...", flush=True)
            subprocess.run(["bash", str(PIPELINE / "transcribe.sh"), str(audio)],
                           env=env, check=True)

        if not reused:
            wav = src.with_suffix(".wav")
            for interpreter, script, input_path, suffix in (
                (ROOT / ".venv-asr/bin/python", "second_opinion.py", wav, ".parakeet.json"),
                (env["WX_PYTHON"], "realign.py", src, ".realigned.json"),
            ):
                # Cached intermediate results need no model environment.
                if not src.with_suffix(suffix).exists():
                    print("  Running the second transcription for comparison..."
                          if script == "second_opinion.py"
                          else "  Refining word timings...", flush=True)
                    subprocess.run([str(interpreter), str(PIPELINE / script),
                                    str(input_path), "--install-dir", str(args.install)],
                                   env=env, check=True)
            print("  Building and installing the transcript...", flush=True)
            subprocess.run([sys.executable, str(PIPELINE / "build_tx.py"),
                            str(src), "--install", str(args.install)], env=env, check=True)

        if not browser_audio.exists():
            print("  Installing playback audio...", flush=True)
            audio_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src.with_suffix(".opus"), browser_audio)
        print(f"  ready: {find_transcript(src.stem, args.install)}", flush=True)
    print("\nPreparation complete. Transcripts and playback audio are ready.\n"
          "To open the app, run: make run", flush=True)


if __name__ == "__main__":
    main()

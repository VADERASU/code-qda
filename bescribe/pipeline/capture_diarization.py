#!/usr/bin/env python3
"""Capture fresh speaker intervals for an isolated trial; never edit transcripts.

Run with the WhisperX environment. In an offline trial, set HF_HUB_OFFLINE=1
and HF_HUB_DISABLE_TELEMETRY=1; PYANNOTE_METRICS_ENABLED=0 disables metrics.
The output is a new run, NOT the historical intervals behind an old transcript.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import time

from backend import torch_device


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--speakers", type=int, required=True)
    ap.add_argument("--model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--revision", required=True)
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"),
                    help="auto selects CUDA when available, otherwise CPU")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--start", type=float, default=0, help="excerpt start, seconds into recording")
    ap.add_argument("--duration", type=float, help="excerpt length in seconds (default: rest of recording)")
    args = ap.parse_args()
    if args.output.exists():
        ap.error(f"refusing to overwrite {args.output}")
    if args.speakers < 1 or args.threads < 1:
        ap.error("speakers and threads must be positive")
    if args.start < 0 or (args.duration is not None and args.duration <= 0):
        ap.error("start must be nonnegative and duration positive")

    import numpy as np
    import random
    import torch
    from pyannote.audio import Pipeline
    from whisperx.audio import load_audio

    args.device = torch_device(args.device)
    if args.device == "cuda" and not torch.cuda.is_available():
        ap.error("CUDA requested but unavailable in this environment; check GPU access and the driver")
    print(f"Device: {args.device}" +
          (f" ({torch.cuda.get_device_name(0)})" if args.device == "cuda" else ""), flush=True)

    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)
    torch.set_num_threads(args.threads)
    started = time.monotonic()
    pipeline = Pipeline.from_pretrained(args.model, revision=args.revision,
                                        token=False).to(torch.device(args.device))
    waveform = torch.from_numpy(load_audio(str(args.audio)))[None, :]
    end = min(waveform.shape[1] / 16000, args.start + args.duration) if args.duration else waveform.shape[1] / 16000
    waveform = waveform[:, round(args.start * 16000):round(end * 16000)]
    if not waveform.shape[1]:
        ap.error("excerpt has no audio")
    progress = {}

    def hook(step, artifact, file=None, total=None, completed=None):
        bucket = int(10 * completed / total) if total and completed is not None else -1
        if progress.get(step) != bucket:
            progress[step] = bucket
            print(f"{step}: {completed}/{total}; {time.monotonic() - started:.0f}s", flush=True)

    result = pipeline({"waveform": waveform, "sample_rate": 16000},
                      num_speakers=args.speakers, hook=hook)

    def turns(annotation):
        return [{"start": s.start + args.start, "end": s.end + args.start, "speaker": label}
                for s, _, label in annotation.itertracks(yield_label=True)]

    digest = hashlib.sha256()
    with args.audio.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    output = {
        "provenance": "fresh diarization; historical intervals were not retained",
        "audio": str(args.audio),
        "audio_sha256": digest.hexdigest(),
        "model": args.model, "revision": args.revision,
        "pyannote_audio": version("pyannote.audio"), "torch": version("torch"),
        "device": args.device, "threads": args.threads, "seed": 0,
        "num_speakers": args.speakers,
        "window": {"start": args.start, "end": end},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "regular": turns(result.speaker_diarization),
        "exclusive": turns(result.exclusive_speaker_diarization),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as fh:
        json.dump(output, fh, indent=2)
        fh.write("\n")
    print(f"saved {args.output} in {output['elapsed_seconds']}s", flush=True)


if __name__ == "__main__":
    main()

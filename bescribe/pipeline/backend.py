"""Shared platform selection; importing this module never loads a model."""
import argparse
import os
import platform


def apple_silicon():
    return platform.system() == "Darwin" and platform.machine().lower() == "arm64"


def select_backend(requested="auto"):
    if requested == "auto":
        return "mlx" if apple_silicon() else "whisperx"
    if requested not in ("mlx", "whisperx"):
        raise ValueError("ASR_BACKEND must be auto, mlx, or whisperx")
    return requested


def torch_device(requested="auto"):
    if requested != "auto":
        return requested
    import torch
    # MPS support varies across alignment/diarization operators. MLX handles
    # Apple GPU transcription; keep these PyTorch stages on CPU by default.
    return "cuda" if torch.cuda.is_available() else "cpu"


def clear_cache(device):
    import torch
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    elif device == "mps":
        torch.mps.empty_cache()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default=os.environ.get("ASR_BACKEND", "auto"),
                    choices=("auto", "mlx", "whisperx"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    backend = select_backend(args.backend)
    if args.check and backend == "mlx" and not apple_silicon():
        ap.error("MLX requires native arm64 Python on an Apple Silicon Mac")
    print(backend)

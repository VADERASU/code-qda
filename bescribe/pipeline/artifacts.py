"""Locate shared transcripts independently of a checkout's paths and mtimes."""
from pathlib import Path
import json
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INSTALL_DIR = ROOT / "md"


def write_json(path, result):
    """Publish a completed stage atomically; an interrupted stage is not cached."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     suffix='.tmp', delete=False) as fh:
        temp = Path(fh.name)
        try:
            json.dump(result, fh, ensure_ascii=False)
            fh.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def transcript_stem(stem):
    """Keep audio preparation and alignment suffixes out of transcript names."""
    return stem.removesuffix(".realigned").removesuffix(".16k")


def transcript_stems(stem):
    """Canonical name first, followed by names used by older pipeline builds."""
    base = transcript_stem(stem)
    return tuple(dict.fromkeys((base, stem, base + ".16k",
                                base + ".realigned", base + ".16k.realigned")))


def find_transcript(stem, *directories):
    # Reuse older names too: they may contain corrections and annotations.
    for directory in directories:
        for candidate in transcript_stems(stem):
            path = Path(directory) / (candidate + ".tx.json")
            if path.exists():
                return path
    return None


def raw_artifact(stem, suffix, directory):
    """Find prepared audio/ASR output after a transcript has been renamed."""
    base = transcript_stem(stem)
    candidates = [Path(directory) / (candidate + suffix)
                  for candidate in dict.fromkeys((stem, base + ".16k", base))]
    return next((path for path in candidates if path.exists()), candidates[0])

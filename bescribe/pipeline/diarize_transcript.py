#!/usr/bin/env python3
"""Assign speakers once and retain the exact intervals used by that assignment."""
import argparse
import json
import os
from pathlib import Path
import sys

from artifacts import ROOT, write_json
from backend import torch_device

sys.path.insert(0, str(ROOT))
from speaker_evidence import remember_basis


def diarize_result(result, audio, device, speakers, model):
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    diarizer = DiarizationPipeline(model_name=model, device=device,
                                  token=os.environ.get('HF_TOKEN'))
    turns, embeddings = diarizer(audio, min_speakers=speakers,
                                 max_speakers=speakers, return_embeddings=True)
    result = whisperx.assign_word_speakers(turns, result, embeddings)
    # JSON parsing separates the flat list from the segment word objects.
    result['word_segments'] = [w for s in result['segments'] for w in s.get('words', [])]
    result['diarization'] = {
        'version': 1, 'source': 'original', 'model': model, 'device': device,
        'turns': [{'start': float(row.start), 'end': float(row.end),
                   'speaker': row.speaker} for row in turns.itertuples()],
    }
    remember_basis(result['segments'])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('--audio', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--speakers', type=int, required=True)
    ap.add_argument('--model', default='pyannote/speaker-diarization-community-1')
    ap.add_argument('--device', default=os.environ.get('TORCH_DEVICE', 'auto'),
                    choices=('auto', 'cpu', 'cuda'))
    args = ap.parse_args()
    if args.speakers < 1:
        ap.error('speaker count must be positive')
    import whisperx
    device = torch_device(args.device)
    print(f'  diarizing on {device} and retaining speaker intervals...', flush=True)
    result = json.loads(args.source.read_text(encoding='utf-8'))
    result = diarize_result(result, whisperx.load_audio(str(args.audio)), device,
                            args.speakers, args.model)
    result.setdefault('models', {})['diarize'] = args.model
    write_json(args.output, result)


if __name__ == '__main__':
    main()

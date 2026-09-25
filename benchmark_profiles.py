"""Select the fastest measured profile that meets explicit quality targets."""
import argparse
import json
import math
from pathlib import Path
from pipeline_state import atomic_json


def choose_profile(records, max_wer, min_recall, max_boundary_errors):
    eligible = []
    for row in records:
        required = ('wer','candidate_recall','boundary_errors','total_seconds')
        if not all(isinstance(row.get(k),(int,float)) and math.isfinite(row[k]) for k in required):
            continue
        if row['wer'] <= max_wer and row['candidate_recall'] >= min_recall and row['boundary_errors'] <= max_boundary_errors and row['total_seconds'] > 0:
            eligible.append(row)
    if not eligible:
        raise ValueError('No measured profile meets the quality targets; current settings retained')
    return min(eligible,key=lambda r:r['total_seconds'])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('measurements',type=Path,help='JSON list of measured profiles and human-labelled quality results')
    parser.add_argument('--max-wer',type=float,required=True)
    parser.add_argument('--min-recall',type=float,required=True)
    parser.add_argument('--max-boundary-errors',type=int,default=0)
    parser.add_argument('--output',type=Path,default=Path('benchmark-selected-profile.json'))
    args=parser.parse_args()
    best=choose_profile(json.loads(args.measurements.read_text(encoding='utf-8')),args.max_wer,args.min_recall,args.max_boundary_errors)
    atomic_json(args.output,best)
    print(f'Saved fastest qualifying measured profile to {args.output}; no unmeasured speed claims or secret changes.')

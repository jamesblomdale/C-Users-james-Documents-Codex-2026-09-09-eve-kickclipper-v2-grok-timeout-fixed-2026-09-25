"""KickClipper-specific candidate and boundary evaluation.

Gold files are intentionally human-authored. This command never calls an API,
transcribes media, or changes production output. Example:

    python evaluate_moments.py --gold eval/gold/example.json \
        --predictions output/vod/NAME/.pipeline/ID/final_candidates.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def overlap_seconds(a: dict, b: dict) -> float:
    return max(0.0, min(float(a["end"]), float(b["end"])) -
               max(float(a["start"]), float(b["start"])))


def temporal_iou(a: dict, b: dict) -> float:
    overlap = overlap_seconds(a, b)
    union = max(float(a["end"]), float(b["end"])) - min(float(a["start"]), float(b["start"]))
    return overlap / union if union > 0 else 0.0


def _best_match(item: dict, choices: list[dict], minimum_iou: float) -> tuple[int | None, float]:
    ranked = [(i, temporal_iou(item, other)) for i, other in enumerate(choices)]
    if not ranked:
        return None, 0.0
    index, score = max(ranked, key=lambda pair: pair[1])
    return (index, score) if score >= minimum_iou else (None, score)


def evaluate(gold: list[dict], predictions: list[dict], *, minimum_iou: float = 0.20) -> dict:
    predictions = sorted(predictions, key=lambda p: float(p.get("score", 0)), reverse=True)

    def at(k: int) -> tuple[float, float]:
        selected = predictions[:k]
        recalled = sum(_best_match(g, selected, minimum_iou)[0] is not None for g in gold)
        useful = sum(_best_match(p, gold, minimum_iou)[0] is not None for p in selected)
        return recalled / len(gold) if gold else 0.0, useful / len(selected) if selected else 0.0

    matched = []
    useful_predictions = 0
    for prediction in predictions:
        index, iou = _best_match(prediction, gold, minimum_iou)
        if index is not None:
            useful_predictions += 1
            target = gold[index]
            matched.append({
                "gold_index": index,
                "iou": iou,
                "start_error_seconds": abs(float(prediction["start"]) - float(target["gold_start"])),
                "end_error_seconds": abs(float(prediction["end"]) - float(target["gold_end"])),
            })

    recall10, precision10 = at(10)
    recall20, precision20 = at(20)
    return {
        "gold_moments": len(gold),
        "predicted_moments": len(predictions),
        "minimum_temporal_iou": minimum_iou,
        "candidate_recall_at_10": recall10,
        "candidate_recall_at_20": recall20,
        "precision_at_10": precision10,
        "precision_at_20": precision20,
        "boundary_start_error_seconds": (
            sum(row["start_error_seconds"] for row in matched) / len(matched) if matched else None
        ),
        "boundary_end_error_seconds": (
            sum(row["end_error_seconds"] for row in matched) / len(matched) if matched else None
        ),
        "useful_clip_rate": useful_predictions / len(predictions) if predictions else 0.0,
        "matched_predictions": len(matched),
    }


def _normalise_gold(rows: list[dict]) -> list[dict]:
    required = {"gold_start", "gold_end", "event_type", "why_it_is_good", "required_context", "payoff_timestamp"}
    result = []
    for index, row in enumerate(rows):
        missing = required - row.keys()
        if missing:
            raise ValueError(f"gold row {index} missing: {', '.join(sorted(missing))}")
        if float(row["gold_end"]) <= float(row["gold_start"]):
            raise ValueError(f"gold row {index} has an invalid range")
        result.append({**row, "start": row["gold_start"], "end": row["gold_end"]})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--minimum-iou", type=float, default=.20)
    parser.add_argument("--output")
    args = parser.parse_args()
    gold_data = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    prediction_data = json.loads(Path(args.predictions).read_text(encoding="utf-8"))
    gold = _normalise_gold(gold_data.get("moments", gold_data))
    predictions = prediction_data.get("candidates", prediction_data)
    report = evaluate(gold, predictions, minimum_iou=args.minimum_iou)
    rendered = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()


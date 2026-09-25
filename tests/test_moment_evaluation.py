import unittest

from evaluate_moments import evaluate, temporal_iou


class MomentEvaluationTests(unittest.TestCase):
    def test_temporal_iou(self):
        self.assertAlmostEqual(temporal_iou({"start": 0, "end": 10}, {"start": 5, "end": 15}), 1/3)

    def test_metrics_and_boundary_errors(self):
        gold = [
            {"start": 10, "end": 20, "gold_start": 10, "gold_end": 20},
            {"start": 40, "end": 55, "gold_start": 40, "gold_end": 55},
        ]
        predictions = [
            {"start": 9, "end": 21, "score": 90},
            {"start": 39, "end": 56, "score": 80},
            {"start": 100, "end": 110, "score": 70},
        ]
        report = evaluate(gold, predictions)
        self.assertEqual(report["candidate_recall_at_10"], 1.0)
        self.assertAlmostEqual(report["precision_at_10"], 2/3)
        self.assertAlmostEqual(report["useful_clip_rate"], 2/3)
        self.assertEqual(report["boundary_start_error_seconds"], 1.0)
        self.assertEqual(report["boundary_end_error_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()

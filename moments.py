"""
The missing piece that was causing real multi-part stories to score as
several separate mediocre fragments instead of one strong clip: this
module merges nearby candidate windows (same topic, or just close
together in time) into a single span, then re-scores that merged text
as one whole moment.

Why this matters: a rambling setup that pays off a window later (an
ego flex whose humbling lands two windows later) will each score
low-to-medium on their own -- neither one is a complete story by
itself. Merged together, the whole arc is what actually scores well
and is what's genuinely worth clipping.

Equally important the other direction: a real peak sitting near weaker
neighbors can still get diluted by the merge, or a peak in the middle
of a long rambling stretch can get glued into one ever-growing group
with no size limit. The grouping rules and peak-preservation fallback
below guard against both.

Scale note: scores are on a 0-100 scale (see detect.py's judgment-
centric scoring system) -- all thresholds in this file are on that
same scale.
"""

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

from detect import score_batch, DetectionResult, ProviderUnavailable
from moment_lexicon import topic_overlap
from config import Config


@dataclass(eq=False)
class Candidate:
    start: float
    end: float
    text: str
    score: float
    reason: str = ""
    topic: str = ""
    moment_type: str = ""  # retired from active logic, kept for structural compatibility
    safety_flag: str | None = None
    breakdown: dict = field(default_factory=dict)
    quote: str = ""
    tightness: float | None = None
    payoff_at_seconds: float | None = None
    payoff_type: str = "none"
    moment_score: float | None = None
    cut_score: float | None = None
    hook_at_seconds: float | None = None
    suggested_trim: str = ""


# How much shared-content-word overlap two topic labels need before
# they're treated as "the same story" for merge purposes. Comparing
# only each label's first word (the old approach) was too fragile --
# "car stuck" and "still no keys" share zero first words but are
# obviously the same unfolding story.
TOPIC_SAME_STORY_OVERLAP = 0.2

# A merged group's total span is capped so a long rambling stretch
# splits into several independently-scored groups instead of one
# diluted blob. 180s (3 min) rather than something tight against a
# single window's own size: DETECTION_WINDOW_SECONDS defaults to 75s,
# and windows overlap heavily (20s hop), so even 2-3 legitimately
# related windows merging together can span well over 100s before any
# rambling dilution is even in play.
MAX_MERGE_SPAN_SECONDS = float(os.getenv('MAX_MERGE_SPAN_SECONDS', '120'))
IRL_MAX_MERGE_SPAN_SECONDS = float(os.getenv('IRL_MAX_MERGE_SPAN_SECONDS', '65'))
IRL_MERGE_GAP_SECONDS = float(os.getenv('IRL_MERGE_GAP_SECONDS', '7'))
IRL_PEAK_NEIGHBOR_FLOOR = float(os.getenv('IRL_PEAK_NEIGHBOR_FLOOR', '55'))
IRL_VERY_HIGH_PEAK_MAX_GAP_SECONDS = float(os.getenv('IRL_VERY_HIGH_PEAK_MAX_GAP_SECONDS', '8'))
IRL_DILUTION_SPLIT_THRESHOLD = float(os.getenv('IRL_DILUTION_SPLIT_THRESHOLD', '12'))
# A topic-overlap "bonus" lets two candidates merge across a somewhat
# larger gap than the base rule allows, when they're clearly the same
# unfolding story -- but time-adjacency (the base MERGE_GAP_SECONDS) is
# still the default, reliable rule.
TOPIC_BONUS_GAP_MULTIPLIER = 1.8
# Two candidates only merge if their raw scores are roughly compatible
# -- either close to each other outright, or both already decent
# (>=50) with matching topics. This is what stops a real 70+ peak from
# getting glued to a <20 neighbor just because they happen to be
# time-adjacent. (0-100 scale.)
SCORE_COMPATIBLE_DELTA = 15.0
SCORE_COMPATIBLE_FLOOR = 50.0
# Hard rule on top of the above: a real peak (>= CLIP_SCORE_THRESHOLD)
# never attaches to a neighbor scoring under this, full stop -- even if
# the general delta/topic rules above would technically allow it at the
# boundary.
PEAK_NEIGHBOR_FLOOR = 45.0
# A VERY high peak (this much or more) needs a tighter standard to merge
# at all -- same topic, neighbor already decent, and genuinely close in
# time -- rather than the normal (looser) gap/topic rules. A 65+ is
# usually a complete moment on its own; only attach something that's
# clearly its direct payoff.
VERY_HIGH_PEAK_SCORE = 65.0
VERY_HIGH_PEAK_NEIGHBOR_FLOOR = 50.0
VERY_HIGH_PEAK_MAX_GAP_SECONDS = 15
# A merged group's re-score is allowed to fall short of the export bar
# if a real individual peak inside it cleared the bar on its own --
# that peak's own data is used instead of the diluted merge result.
# This is a flat, generic lower bar -- the old system gave a lower bar
# only to certain "moment types" (REVEAL/QUOTE_BOMB/etc.), but the new
# judgment-centric scoring system doesn't have a type taxonomy, so this
# applies uniformly: a clean peak just under the main bar is still
# often worth posting.
PEAK_FALLBACK_LOWER_BAR_SCORE = 55.0
# Even when a merged group DOES clear the export bar on the combined
# re-score, a big gap between the group's best window and its average
# means the merge is likely padded with weaker material around a real
# moment. Above this gap, prefer exporting the peak window's own
# tighter span instead of the full (partly diluted) merged span -- a
# cleaner cut, not just a passing one.
MAX_MINUS_MEAN_SPLIT_THRESHOLD = 20.0


def _scores_compatible(a_score: float, b_score: float, topic_same: bool) -> bool:
    if a_score >= Config.CLIP_SCORE_THRESHOLD and b_score < PEAK_NEIGHBOR_FLOOR:
        return False
    if b_score >= Config.CLIP_SCORE_THRESHOLD and a_score < PEAK_NEIGHBOR_FLOOR:
        return False
    if abs(a_score - b_score) <= SCORE_COMPATIBLE_DELTA:
        return True
    return topic_same and a_score >= SCORE_COMPATIBLE_FLOOR and b_score >= SCORE_COMPATIBLE_FLOOR


def merge_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """
    Groups nearby candidates together, then re-scores each merged
    group's full combined text as one moment. A candidate only joins a
    group if it's time-adjacent (or topic-similar across a slightly
    larger gap) AND its score is roughly compatible with what's already
    in the group -- this is what stops a real peak from being glued to
    weak neighbors and diluted below the export bar. A hard span cap
    stops any single group from growing without bound.

    If a merged group's re-scored result still comes back below the
    export threshold despite all that, but one of its ORIGINAL
    candidates individually cleared the bar (or the lower fallback
    bar), that individual peak's own data is returned instead of the
    diluted merge -- a real moment should never disappear just because
    the surrounding re-score came out soft.
    """
    if not candidates:
        return []

    candidates = sorted(candidates, key=lambda c: c.start)
    groups: list[list[Candidate]] = [[candidates[0]]]

    for c in candidates[1:]:
        prev = groups[-1][-1]
        gap = c.start - prev.end
        group_start = groups[-1][0].start
        span_if_joined = c.end - group_start

        same_topic = topic_overlap(c.topic, prev.topic) >= TOPIC_SAME_STORY_OVERLAP

        irl_group = str(prev.moment_type).upper().startswith("IRL_") or str(c.moment_type).upper().startswith("IRL_") or str(prev.moment_type).upper() in {"CHAOS","STATUS"} or str(c.moment_type).upper() in {"CHAOS","STATUS"}
        span_cap = IRL_MAX_MERGE_SPAN_SECONDS if irl_group else MAX_MERGE_SPAN_SECONDS
        base_gap = IRL_MERGE_GAP_SECONDS if irl_group else Config.MERGE_GAP_SECONDS
        peak_neighbor_floor = IRL_PEAK_NEIGHBOR_FLOOR if irl_group else PEAK_NEIGHBOR_FLOOR
        within_span_cap = span_if_joined <= span_cap

        # A very high peak needs a tighter standard to merge at all --
        # only its clear, close, same-topic payoff, not the normal
        # looser gap/topic rules.
        involves_very_high_peak = prev.score >= VERY_HIGH_PEAK_SCORE or c.score >= VERY_HIGH_PEAK_SCORE
        if involves_very_high_peak:
            close_enough = gap <= (IRL_VERY_HIGH_PEAK_MAX_GAP_SECONDS if irl_group else VERY_HIGH_PEAK_MAX_GAP_SECONDS)
            compatible = same_topic and c.score >= (IRL_PEAK_NEIGHBOR_FLOOR if irl_group else VERY_HIGH_PEAK_NEIGHBOR_FLOOR) and prev.score >= (IRL_PEAK_NEIGHBOR_FLOOR if irl_group else VERY_HIGH_PEAK_NEIGHBOR_FLOOR)
        else:
            within_base_gap = gap <= base_gap
            within_bonus_gap = gap <= base_gap * TOPIC_BONUS_GAP_MULTIPLIER and same_topic
            close_enough = within_base_gap or within_bonus_gap
            compatible = _scores_compatible(c.score, prev.score, same_topic)

        worth_joining = within_span_cap and close_enough and compatible

        if worth_joining:
            groups[-1].append(c)
        else:
            groups.append([c])

    # The old implementation called score_window once per group, in a
    # serial loop. A three-hour VOD can produce 70-100 groups, making this
    # otherwise small merge pass take 20-30 minutes of pure HTTP latency.
    # Score the same text with the existing batch prompt and bounded pool.
    # Results remain aligned to groups, so this changes transport cost only,
    # not the merge/peak-preservation policy below.
    batch_size = max(1, min(12, int(os.getenv("MERGE_WINDOWS_PER_BATCH", "8"))))
    workers = max(1, min(8, int(os.getenv(
        "MERGE_SCORING_MAX_CONCURRENCY",
        str(getattr(Config, "DEEP_ANALYSIS_MAX_CONCURRENCY", 4)),
    ))))
    indexed_batches = [
        (i, groups[i:i + batch_size]) for i in range(0, len(groups), batch_size)
    ]
    results_by_group = [None] * len(groups)

    def score_groups(start, batch):
        return start, score_batch([" ".join(g.text for g in group) for group in batch])

    print(f"[moments] re-scoring {len(groups)} merged groups in "
          f"{len(indexed_batches)} batches; {workers} concurrent", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(score_groups, start, batch) for start, batch in indexed_batches]
        for future in as_completed(futures):
            try:
                start, batch_results = future.result()
            except ProviderUnavailable:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                print(f"[moments] WARNING merged batch failed ({type(exc).__name__}); "
                      "peak fallback will preserve already-qualified moments", flush=True)
                continue
            for offset, result in enumerate(batch_results):
                if start + offset < len(results_by_group):
                    results_by_group[start + offset] = result

    merged = []
    for group, result in zip(groups, results_by_group):
        combined_text = " ".join(g.text for g in group)

        peak = max(group, key=lambda g: g.score)

        if result is None:
            # The merged re-score failed (API error/parse error). If the
            # group's best individual candidate already cleared the real
            # bar on its own, fall back to that rather than losing it
            # over an unrelated API hiccup.
            if peak.score >= Config.CLIP_SCORE_THRESHOLD:
                print(f"[moments] merged re-score failed for {group[0].start:.0f}s-{group[-1].end:.0f}s, "
                      f"but the group's own peak ({peak.score}) already cleared the bar -- using it directly")
                merged.append(peak)
            else:
                print(f"[moments] merged group re-score failed, dropping "
                      f"{group[0].start:.0f}s-{group[-1].end:.0f}s")
            continue

        final_candidate = Candidate(
            start=group[0].start,
            end=group[-1].end,
            text=combined_text,
            score=result.score,
            reason=result.reason,
            topic=group[0].topic,
            safety_flag=result.safety_flag,
            breakdown=result.breakdown,
            quote=result.quote,
            moment_type=getattr(result, 'event_class', ''),
            tightness=getattr(result, 'tightness', None),
            payoff_at_seconds=getattr(result, 'payoff_at_seconds', None),
            payoff_type=getattr(result, 'payoff_type', 'none'),
        )

        cleared_normal_bar = result.score >= Config.CLIP_SCORE_THRESHOLD
        cleared_peak_fallback = peak.score >= PEAK_FALLBACK_LOWER_BAR_SCORE
        mean_raw = sum(g.score for g in group) / len(group)
        is_irl = str(peak.moment_type).upper().startswith("IRL_") or str(peak.moment_type).upper() in {"CHAOS","STATUS"}
        dilution_threshold = IRL_DILUTION_SPLIT_THRESHOLD if is_irl else MAX_MINUS_MEAN_SPLIT_THRESHOLD
        big_dilution_gap = (peak.score - mean_raw) >= dilution_threshold

        print(f"[moments] group {group[0].start:.0f}s-{group[-1].end:.0f}s: "
              f"merged_score={result.score} max_raw={peak.score} mean_raw={mean_raw:.1f} "
              f"-> {'keep' if (cleared_normal_bar or cleared_peak_fallback) else 'drop'}")

        if not (cleared_normal_bar or cleared_peak_fallback):
            continue  # genuinely didn't clear either bar, correctly dropped

        if len(group) > 1 and big_dilution_gap:
            # Even though this group clears the bar, it's clearing it
            # while carrying real padding around the actual moment --
            # export the peak window's own tighter span instead of the
            # full merged one for a cleaner cut.
            print(f"[moments] max-mean gap ({peak.score - mean_raw:.1f}) is large -- "
                  f"exporting the peak window alone ({peak.start:.0f}-{peak.end:.0f}s) instead of the full merged span")
            merged.append(peak)
        elif cleared_normal_bar:
            merged.append(final_candidate)
        else:
            # cleared only via the peak fallback (merged re-score itself
            # didn't clear the bar) -- use the peak's own data, same as
            # the big-dilution-gap case above.
            print(f"[moments] merged score ({result.score}) diluted a real peak "
                  f"({peak.score}) -- using the peak directly instead of the merge")
            merged.append(peak)

    return merged


def dedupe_clips(clips: list[Candidate], min_overlap: float = 0.45, max_center_gap: float = 18.0) -> list[Candidate]:
    """
    Safety net that runs AFTER merge_candidates(): even with topic/gap
    merging, it's still possible for what's really the same moment to
    end up as two separate merged clips -- e.g. if the LLM assigned
    slightly different topic labels to the same story on either side of
    a gap larger than MERGE_GAP_SECONDS. This catches those remaining
    duplicates by comparing time overlap and how close their midpoints
    are, keeping only the higher-scoring one of any pair that's clearly
    the same moment. Prevents wasting a render slot (and a title-
    generation call) on a near-duplicate of a clip you're already making.
    """
    if not clips:
        return []

    ranked = sorted(clips, key=lambda c: c.score, reverse=True)
    kept: list[Candidate] = []

    def overlap_fraction(a: Candidate, b: Candidate) -> float:
        inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
        shorter = min(a.end - a.start, b.end - b.start)
        return inter / shorter if shorter > 0 else 0.0

    def center(c: Candidate) -> float:
        return (c.start + c.end) / 2

    for c in ranked:
        is_duplicate = False
        for k in kept:
            same_time = overlap_fraction(c, k) >= min_overlap or abs(center(c) - center(k)) <= max_center_gap
            if same_time:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(c)

    return sorted(kept, key=lambda c: c.start)

# IRL-aware moment detection refinement

This build keeps STABLE-8's proven scout -> deep score -> merge -> peak protection -> payoff cap -> whole-VOD rank architecture, while fixing IRL blind spots.

Implemented:
- explicit event classes: IRL_ACTION, IRL_REACTION, QUOTE, CONFRONTATION, REVEAL, COMEDY, FLEX_FAIL, HEART, STATUS, CHAOS
- class-aware scoring: talk keeps the existing weights; IRL shifts weight toward payoff/emotion/novelty and away from judgment
- separate `tightness` (0-100) cut-quality score; final score blends 70% editorial / 30% tightness; tightness <60 cannot score 80+
- `payoff_at_seconds` + `payoff_type` fields in deep scoring/review
- review-time payoff boundary validation; unsupported/out-of-cut payoff is capped below 50
- IRL merge defaults: 65s max span, 7s gap, 55 neighbor floor, 8s very-high-peak gap
- IRL dilution split threshold tightened from 20 to 12
- high-recall IRL scout lexicon and thin-speech bypass for short reaction/action windows
- whole-VOD ranker now explicitly prioritizes variety and penalizes padded IRL cuts
- checkpoint metadata now records event class, tightness and payoff fields

Not faked/overclaimed:
- This patch does NOT claim transcript text can see physical action. Visual-only IRL payoff still needs actual visual review to verify it.
- A true pre-deep-score frame sampler/chat-spike detector would require threading video/chat sources into the scout stage. That is a larger architecture change and is not silently simulated here.
- Export threshold remains 60; candidate threshold remains 32; peak lock remains 65; payoff <3 cap remains 49.

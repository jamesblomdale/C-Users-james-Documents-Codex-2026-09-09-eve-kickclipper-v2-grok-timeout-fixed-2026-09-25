# Fast / Quality profiles

## FAST (default)

- Together Parakeet is transcription-only.
- OpenAI `gpt-5.4-mini` is the scout/finder with reasoning off.
- Grok 4.6 judge is disabled so a six-hour VOD is not held up by a second LLM pass.
- Eight simple final clips maximum, tracking and context review disabled.

## QUALITY (optional)

Set `JUDGE_ENABLED=true`, `JUDGE_MODEL=grok-4.6`, and
`CUSTOMER_MAX_CLIPS_PER_JOB=12`. The judge is intended to review only merged
survivors, never the full transcript or every scout window. Keep
`JUDGE_REASONING_EFFORT=medium` or `low`; never use high by default.

The provider roles remain separate: Together/Parakeet handles ASR, OpenAI
handles discovery, and xAI/Grok handles optional survivor taste review.

# Kick Clipper SaaS architecture update — product requirements integrated

## Product target

The launch product is an AI editor for Kick, Twitch, YouTube and uploaded video files. It supports recorded VODs and user-supplied live stream links. Its competitive target is: stronger moment detection (especially storytelling, arguments, shocking statements, humour, emotion, controversy and drama), faster processing than StreamLadder, and better live clipping than Supo while keeping transcript accuracy and reliability high.

Launch goals:
- 20+ useful candidate moments on long streams when enough real moments exist; never fabricate filler just to hit a quota.
- Most clips under 3 minutes unless the story genuinely needs longer.
- English first.
- Speaker separation must preserve who said what; person_1/person_2 labels are enough.
- Optional selective visual reasoning, audio/event signals and chat context can strengthen candidates, but semantic transcript understanding stays primary.
- Progressive results: good candidates can appear before the full VOD finishes.
- Optional captions, vertical reframing, face/speaker tracking, layouts and templates.
- Searchable transcript + manual transcript-to-clip workflow is a target capability.
- Desktop-first launch; direct social publishing is a later phase.

## Performance target and what it really means

For a six-hour source, the business goal is under 10 minutes and a hard target of roughly 15 minutes end-to-end where infrastructure/provider limits permit it. This is a target, not a guarantee. Separate metrics must be tracked for:

1. time-to-first-candidate,
2. time-to-all-moments-ranked,
3. time-to-first-render,
4. time-to-all-requested-renders.

Moment discovery can be much faster than producing 20 captioned/reframed renders. The system must report these independently rather than hiding render time inside a single vague percentage.

## Core processing architecture

The existing long-VOD upgrade remains the foundation:

source -> audio-first ingest -> overlapping ASR chunks -> bounded parallel transcription -> absolute word timestamps -> overlap reconciliation -> semantic windows independent from ASR chunk boundaries -> progressive high-recall scout -> deep candidate reasoning -> targeted low-confidence transcript repair -> selective visual/audio verification -> dedupe/rank -> retrieve/render only selected video ranges.

Important retained properties:
- ASR chunks exist for throughput; semantic windows exist for understanding. They are deliberately different.
- A small overlap prevents sentence loss at ASR boundaries.
- A larger semantic overlap prevents stories from being cut at analysis boundaries.
- Cheap work is parallelized; expensive work is reduced.
- Full-resolution visual analysis is never run over an entire multi-hour stream by default.
- Completed ASR/checkpoints are reusable so changing clip settings does not retranscribe the source.
- Failed chunks retry independently.
- Jobs checkpoint and resume instead of restarting from zero.

## Moment-quality policy

Primary editorial dimensions remain oriented to the intended creator/editor audience:
- judgment / controversy: 30%
- comment/debate potential: 25%
- hook strength: 20%
- reaction/emotion: 15%
- standalone understanding: 10%

Payoff quality is evaluated separately and can cap weak endings. Storytelling must not be accidentally penalized just because a setup is quieter than a reaction. Audio energy, interruptions, laughter/shouting, visual motion/reactions and chat spikes are supporting evidence, not replacements for semantic reasoning.

The scout should optimize for recall: it is better to send a plausible event to the second pass than to permanently miss it. The deep pass then optimizes precision, boundaries, context completeness and posting value.

## Source/platform policy

VOD launch sources:
- Kick URL
- Twitch URL
- YouTube URL
- authenticated user upload

Live launch sources:
- Kick live URL or legacy bare Kick channel name
- Twitch live URL
- YouTube live URL

Customer-provided arbitrary server file paths are rejected. Uploaded files are saved inside that customer's own upload namespace and only those paths are accepted by the processing route. Raw arbitrary HLS input remains owner/debug-only.

The live capture layer now accepts a generic streamlink-compatible source rather than constructing a Kick URL internally. Actual provider behavior must still be integration-tested before claiming production support for every live variant (private/member streams, age gates, expiring tokens and platform changes are provider-specific).

## SaaS policy added

- New-user free allowance defaults to 180 source-minute credits (3 hours).
- Customer generated artifacts default to a five-day retention policy.
- Re-analysis should reuse cached transcript/timestamps/features rather than spend credits on retranscription.
- Owner/admin remains unrestricted for development/operations.
- Users can start multiple jobs; the local build emits an overload warning after the soft threshold and enforces a hard process safety cap. A production cloud deployment should queue/autoscale instead of spawning unlimited local subprocesses.
- Launch source-duration guardrail defaults to 24 hours for customers. Owner jobs can bypass it. This can become plan-dependent in the cloud scheduler later.
- Optional SMTP completion email plumbing is included; browser completion notifications belong in the dashboard/client layer.
- Customers receive normal progress but internal logs/guardian diagnostics are owner-only.
- Cost protection must stay above user convenience: bounded ASR/AI/render concurrency, max upload size, disk checks, request retries and cancellation remain required.

## Retention

`retention.py` provides a dry-run-first cleanup command. Default is five days. It intentionally does not silently delete owner output at app startup.

Dry run:

    python retention.py

Apply:

    python retention.py --apply

Production should schedule this daily through the deployment scheduler/cron rather than running destructive cleanup during a web request.

## Cloud scaling implication

The current ZIP is still a single-host Flask application spawning local subprocesses. That is suitable for development/beta, NOT 100+ simultaneous long jobs.

Before a 1,000-user launch, separate the following operational roles:
- web/API service
- durable job queue
- ingest workers
- ASR workers
- scout/deep-analysis workers
- render workers
- object storage
- database
- notification worker

The media modules should remain usable by workers; do not rewrite ASR/moment logic just to move it behind a queue.

A production queue should support:
- per-account concurrency allowances
- priority by paid tier only when commercially needed
- global provider rate limits
- worker capability labels (CPU/GPU/render)
- retry + dead-letter handling
- resume from existing checkpoints
- cancellation
- cost ceilings per job/account
- autoscaling with a strict maximum spend ceiling

The early AUD $100–300/month budget means infrastructure must scale with revenue. Do not provision capacity for 1,000 simultaneous long jobs before paying usage exists.

## Credits / billing direction

Keep one simple customer-facing credit balance, but internally measure a usage ledger for:
- source duration
- transcription/API usage
- reasoning usage
- visual review usage
- render/GPU usage
- storage

This allows pricing to change without coupling media algorithms to billing. Processing hours should not become a separate confusing visible currency when the user has chosen a combined credit/subscription/clip model.

## New/changed product configuration

- `FREE_TRIAL_CREDITS=180`
- `RETENTION_DAYS=5`
- `SOFT_CONCURRENT_JOB_WARNING=3`
- `HARD_LOCAL_JOB_CAP=8`
- `MAX_SOURCE_HOURS=24` (`0` means unlimited)
- `MAX_UPLOAD_GB=12`
- `LIVE_SOURCE` (set per live job)
- optional `SMTP_*` settings

These are launch defaults, not benchmark-proven optimums.

## Security/reliability implications

- Never accept arbitrary customer filesystem paths.
- Never expose platform API keys to the browser.
- Never use customer-provided raw HLS except via an explicitly controlled path.
- Never make an unlimited local process count equal to a paid plan promise.
- Do not delete artifacts on startup; use scheduled retention.
- Keep transcript/checkpoint caches identity-bound to the exact source/provider/config.
- Keep provider retries bounded.
- One failed ASR chunk must not destroy a 24-hour job.
- A server restart must reuse completed checkpoints.
- Keep render concurrency separate from ASR/analysis concurrency.
- Validate disk limits before large jobs/uploads.

## What still needs production work

This update deliberately does not fake systems that need deployment decisions or third-party credentials:
- distributed cloud queue/autoscaling
- real subscription plans and per-plan concurrency
- cloud object storage
- production email provider credentials
- Google/OAuth login
- direct social publishing
- live-chat ingestion for all providers
- calibrated multi-provider ASR benchmark selection
- true speaker diarization in every transcription provider
- 100+ simultaneous-job load test
- six-hour real-world under-10-minute benchmark

Those are explicit next phases rather than hidden gaps.

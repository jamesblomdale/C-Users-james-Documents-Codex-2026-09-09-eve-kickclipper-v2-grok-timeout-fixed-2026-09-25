#!/usr/bin/env bash
set -euo pipefail

# Run from the project root. This preserves secret values, backs up .env, and
# removes stale duplicate model/provider lines before appending one canonical
# launch profile. It never prints secret values.
test -f .env || cp .env.example .env
cp .env ".env.backup.$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp)"
grep -vE '^(LLM_PROVIDER|LLM_API_KEY|OPENAI_SCORING_MODEL|OPENAI_TITLE_MODEL|GROK_SCORING_MODEL|GROK_TITLE_MODEL|SCOUT_MODEL|DEEP_SCORING_MODEL|SCOUT_MAX_CONCURRENCY|SCOUT_REASONING|JUDGE_MODEL|JUDGE_ENABLED)=' .env > "$tmp" || true
cat >> "$tmp" <<'EOF'

# Canonical fast launch profile. Insert keys manually; do not commit .env.
LLM_PROVIDER=openai
LLM_API_KEY=
OPENAI_SCORING_MODEL=gpt-5.4-mini
OPENAI_TITLE_MODEL=gpt-5.4-mini
SCOUT_MODEL=gpt-5.4-mini
DEEP_SCORING_MODEL=gpt-5.4-mini
SCOUT_REASONING=off
SCOUT_MAX_CONCURRENCY=4
TRANSCRIPTION_PROVIDER=parakeet
ASR_PROVIDER=parakeet
TRANSCRIPTION_FALLBACK=none
WHISPER_FALLBACK=false
PARAKEET_CONCURRENCY=8
JUDGE_MODEL=grok-4.6
JUDGE_ENABLED=true
JUDGE_REASONING_EFFORT=low
JUDGE_MAX_CONCURRENCY=3
EOF
mv "$tmp" .env
chmod 600 .env
echo "Repaired .env; backup created. Add/rotate secrets in nano .env, then restart the server."

ROLLING_REMOTE_ASR=true

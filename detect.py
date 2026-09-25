"""
Scores rolling transcript windows for clip-worthiness using an LLM, with
a full weighted breakdown (not just one number) so you can see WHY a
moment scored what it did -- and a safety flag pass so borderline
content (minors, explicit, unverified serious claims) gets caught before
it's ever cut, not after.

SCORING PHILOSOPHY v4 (source of truth): this is tuned for ONE specific
audience -- edgy, internet-native, US-based X/Twitter viewers who are
happy to roast, mock, judge, debate, and pile on. The system does not
ask "is this viral/funny/controversial." It asks, in order: WHO is
going to get judged here -> WHY will people judge them -> WHAT will
people actually comment -> HOW MANY different comment angles are there
-> IS there a payoff -> can this become a clean, short clip.

v4 changes from v3: JUDGMENT and COMMENT_POTENTIAL are now separate,
co-equal 40%-weight categories (not one 35% judgment + a 15% "criticism
potential" folded in) -- judgment is "does the footage make someone
judgeable," comment potential is "how much would people actually reply
about it" (angles, roastability, debate, argument, variety, ease -- see
COMMENT_POTENTIAL below). Both are now hard gates (see _weighted_score):
weak judgment OR weak comment potential caps the score low, regardless
of how good everything else is. The judgment TARGET is no longer
assumed to be the streamer -- a side character, a guest, or someone
else entirely can be who the audience actually judges; the model is
asked to identify the target explicitly rather than defaulting to the
streamer.

This is text-only, so it's cheap -- fractions of a cent per call even
running continuously through a multi-hour stream. Two-tier LLM cost is
already built in elsewhere: GROK_SCORING_MODEL (cheap/fast) scores
every window here; GROK_TITLE_MODEL (stronger/more expensive) is only
ever invoked in generate_posts.py, once per clip that already survived
this scoring pass -- i.e. "deep analysis only for top candidates" is
already the shape of the pipeline, not something layered on top of it.
"""

import json
import time
import os
import threading
import requests
from dataclasses import dataclass, field
from config import Config
from moment_lexicon import has_action, has_judgment_signal, name_only_spam

if Config.DISABLE_ENV_PROXY and not Config.NETWORK_PROXY:
    for _proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_proxy_name, None)

GROK_URL = "https://api.x.ai/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Weighted scoring categories -- weights must sum to 1.0. JUDGMENT and
# COMMENT_POTENTIAL are co-equal at 40% each and BOTH hard gates (see
# _weighted_score) -- a great hook or clean payoff must never rescue a
# clip that's weak on either. Priority order: JUDGMENT = COMMENT_
# POTENTIAL > PAYOFF > STANDALONE = HOOK > TECHNICAL.
CATEGORY_WEIGHTS = {
    "judgment": 0.20,            # roast/debate/hypocrisy/ego value
    "comment_potential": 0.20,   # how naturally people will reply/disagree/joke
    "payoff": 0.15,              # reveal/reaction/consequence/resolution
    "hook": 0.15,                # immediate reason to keep watching
    "standalone": 0.10,          # understandable without outside context
    "novelty": 0.10,             # specific/unexpected/rare streamer moment
    "emotion": 0.08,             # tension, laughter, shock, warmth, embarrassment, etc.
    "technical": 0.02,           # transcript clarity / usable source
}

DETECTION_PROMPT = """You score Kick/IRL transcript windows for ONE audience: edgy, \
internet-native, US-based X/Twitter viewers who are highly willing to roast, mock, laugh at, \
criticize, debate, and judge people in streamer content. The goal is NOT to manufacture \
controversy or harassment -- it's to find GENUINE footage where someone's own words, \
actions, reactions, or situation naturally give this audience a real reason to say \
something. The target is not always the streamer -- identify who it actually is.

STREAMER is {streamer_name}. KNOWN_NAMES below are spellings only -- do not raise any score \
just because a name was spoken.

KNOWN_NAMES (spelling reference ONLY): {priority_names}

Work through this silently, in order, before scoring:
1. What actually happens in this window?
2. Who is involved, and who is doing/saying the important thing? (streamer, a friend, a \
guest, a stranger, another creator, anyone else materially present)
3. Who is the PRIMARY_JUDGMENT_TARGET -- the person this audience would actually judge? \
Set judgment_target to STREAMER, SIDE_CHARACTER (someone else in the clip), BOTH, or OTHER. \
Do NOT default to the streamer just because they're the one streaming -- if a guest brags \
and gets humbled, the guest is the target, not the streamer for merely being present.
4. What would this specific audience's reaction be to that person? Fill predicted_reactions \
with 5-10 short, casual hypothetical X replies BEFORE assigning any numeric score -- the \
scores must be grounded in what you actually predicted, not the other way around.
5. What is the main negative judgment available, and does the footage ITSELF support it (not \
a caption inventing it)?
6. Is there a payoff/consequence?
Then assign scores.

THE CORE QUESTION is: "Does this window contain a specific moment a Kick-clips viewer would \
stop for, understand, and feel compelled to watch through or react to WITHOUT a misleading \
caption?" Judgment/roast clips are important, but they are NOT the only valid path. Also score \
strongly when the footage contains genuine surprise/chaos, an awkward social moment, a funny \
exchange, a strong personal take or life lesson, an emotional reveal, a flex/status moment, a \
confrontation, a sudden demeanor change, or a satisfying reveal/payoff. Do not force every good \
clip into a negative roast angle.

Score 1-10 on each of these:

JUDGMENT: does the judgment target do or say something that hands this audience a genuine, \
natural reason to judge them? Look across: EGO (arrogant, cocky, superior, untouchable, \
establishing dominance, demanding special treatment), CRINGE (embarrassing, trying too hard, \
forced/awkward, desperate for attention, socially unaware), STUPIDITY (confidently wrong, \
terrible decision, ridiculous logic, doubles down after being wrong, a failed prediction), \
DELUSION (unrealistic self-image, refuses obvious reality, words contradicted by what's \
shown, zero self-awareness), ENTITLEMENT (expects special treatment, thinks rules don't \
apply, superior because of money/status), HYPOCRISY (says one thing, does another; changes \
standards when convenient; claims humility while bragging), DISRESPECT (talks down to, \
humiliates, or insults someone), MONEY/STATUS (flexing wealth, luxury, calling someone \
broke, gambling, reckless spending, a financial loss right after a flex), SOCIAL DRAMA \
(rejection, friendship fallout, relationship conflict, betrayal, an awkward reunion, getting \
kicked out), CALLOUTS (attacking another creator, calling someone fake/overrated, \
questioning their intelligence or authenticity, a superiority claim), and CONTROVERSIAL \
STATEMENTS (politics, immigration, policing, religion, gender, morality, culture, \
relationships) -- but controversy is ONLY judgment material if the TARGET becomes the object \
of judgment for how/what they said (out of touch, clueless, insane), never just because the \
topic itself is heavy. It's much stronger if it contradicts the target's own established \
persona (an "I don't care" streamer visibly furious at criticism; a self-described "humble" \
streamer bragging for minutes; an "unbeatable" claim immediately followed by failure) -- that \
contradiction adds an EXTRA judgment: zero self-awareness. Score 1-3, ALWAYS, if: the target \
is merely reacting to someone else's action; they're only mentioned, not shown acting; \
someone ELSE is the actual judgeable party; the "controversy" only exists via how a caption \
could frame it; understanding needs context outside this window; the target isn't clearly \
responsible; or it's generic funny/gameplay/wholesome/motivational content with nothing to \
judge.

COMMENT_POTENTIAL: how likely is THIS audience to actually reply with something specific, not just \
scroll past? Evaluate: COMMENT ANGLES (how many genuinely different things could people \
say -- arrogant AND stupid AND delusional is three angles, embarrassing alone is one; 1 \
angle = mid score, 2 = strong, 3+ = max score), ROASTABILITY (is there an obvious, specific \
thing to make fun of), DEBATE (could people reasonably disagree with each other about it), \
ARGUMENT (could replies plausibly turn into a back-and-forth), COMMENT VARIETY (can people \
make jokes, criticism, observations, memes, comparisons, defenses -- not just one reaction \
type), COMMENT EASE (can a viewer instantly think of something specific to say, with zero \
extra thought). Do NOT count generic non-judgmental noise ("LOL," "W," "L," "bro," "crazy," \
"💀," "😭" by themselves) as real comment potential -- score high only when replies would \
likely contain an actual opinion (cringe, arrogant, delusional, ego insane, he deserved that, \
zero self awareness, entitled, hypocrite, he's mad because he's wrong, etc.), not just \
entertainment noise.

PAYOFF: does the judgment moment actually CLOSE, not just set up? Prefer: ego->humbling, \
brag->failure, money flex->loss, confident claim->proven wrong, disrespect->consequence, \
hypocrisy->exposed, lie->caught, cringe->awkward reaction, flirting->rejection, \
callout->confrontation, controversial statement->defensive reaction, friendship \
fallout->awkward interaction, status claim->reality check. A setup that pays off outside \
this window is NOT a payoff -- score it low even if the setup itself is strong. A payoff is \
not mandatory, but a strong one should dramatically increase this score specifically.

STANDALONE: would a viewer with ZERO prior context instantly understand who's doing what, \
what happened, and why it's judgeable? A random viewer needing five minutes of backstory is \
low value; instant understanding is high value.

HOOK: does the first 1-3 seconds immediately create a clear reason to keep watching around the \
actual moment? Strong hooks include conflict, a bold claim, an unusual action, a visible reaction, \
a money/status flex, an awkward setup, a reveal, or an unanswered "why/what happens next".

NOVELTY: is this specific enough to feel different from routine streamer filler? Reward unusual \
combinations, unexpected behavior, rare interactions, distinctive claims, surprising reversals, \
or a familiar streamer behaving in an unexpected way. Do not reward a famous name by itself.

EMOTION: how much real feeling is present or naturally produced by the moment -- tension, shock, \
laughter, embarrassment, anger, warmth, excitement, fear, disbelief, awkwardness, or sincerity. \
Do not invent facial reactions that are not in the transcript/context.

TECHNICAL: clear audio/video, the judgment target visible when appropriate, no major \
clipping/cropping issues, easy to follow. Deliberately the lowest-weighted category -- \
content quality matters far more than technical polish.

Do NOT artificially lower judgment/comment_potential just because a strong clip is wholesome, \
funny, surprising, emotional, impressive, or advice-driven. Score those two categories honestly, \
then let HOOK/PAYOFF/NOVELTY/EMOTION carry the clip when that is the real reason it works.

Also flag safety concerns if present: does this window involve a minor/child, explicit \
sexual content, or a serious unverified accusation/claim (crime, abuse, etc.) that \
shouldn't be clipped without real verification? Set safety_flag to a short label if so \
("minor_present", "explicit_content", "unverified_serious_claim"), or null if none apply.

Also return:
- judgment_target: "STREAMER", "SIDE_CHARACTER", "BOTH", or "OTHER" -- see step 3 above.
- predicted_reactions: 5-10 SHORT hypothetical X/Twitter replies a real viewer might post \
about the judgment target after watching just this clip (lowercase, casual, the way people \
actually type -- e.g. "his ego is insane", "bro thought he was him", "this is so cringe", "he \
deserved that", "zero self awareness"). Fill this in FIRST, before the numeric scores. If you \
can't honestly generate real judgmental reactions (only generic "lol"/"W"/"L" energy), that \
itself means both judgment and comment_potential should score low.
- topic: a short 3-6 word label for what this window is about -- used to detect when \
nearby windows are part of the same moment
- quote: the single best, most quotable line, copied EXACTLY as it appears in the transcript \
below -- or an empty string if nothing in this window is worth quoting. Never paraphrase or \
invent a quote; it must be a literal substring of the transcript.

Transcript window:
\"\"\"
{transcript}
\"\"\"

Respond with ONLY a JSON object, no other text:
{{
  "judgment_target": "STREAMER|SIDE_CHARACTER|BOTH|OTHER",
  "predicted_reactions": ["<short reaction>", "..."],
  "judgment": <1-10>,
  "comment_potential": <1-10>,
  "payoff": <1-10>,
  "standalone": <1-10>,
  "hook": <1-10>,
  "novelty": <1-10>,
  "emotion": <1-10>,
  "technical": <1-10>,
  "safety_flag": <string or null>,
  "topic": "<short label>",
  "quote": "<exact substring from the transcript, or empty>",
  "reason": "<one short sentence on the standout moment, no banned names unless they appear in this window>"
}}
"""


@dataclass
class DetectionResult:
    score: float                    # weighted 0-100 total (see _weighted_score)
    breakdown: dict = field(default_factory=dict)  # category -> raw 1-10
    reason: str = ""
    safety_flag: str | None = None
    topic: str = ""
    moment_type: str = ""  # retired from active scoring logic, kept for structural
                            # compatibility with anything that still reads this field
    quote: str = ""
    judgment_target: str = ""  # STREAMER | SIDE_CHARACTER | BOTH | OTHER | "" (unset)
    event_class: str = ""
    tightness: float | None = None
    payoff_at_seconds: float | None = None
    payoff_type: str = "none"

    @property
    def tier(self) -> str:
        if self.score >= 90:
            return "Post immediately"
        if self.score >= 75:
            return "Strong, post today"
        if self.score >= 60:
            return "Decent, good for volume"
        if self.score >= 40:
            return "Only if you need filler"
        return "Skip"


def _apply_hard_caps(breakdown):
    import math
    result = {k: float(breakdown[k]) for k in CATEGORY_WEIGHTS}
    if any(not math.isfinite(v) or not 0 <= v <= 10 for v in result.values()):
        raise ValueError("Scorer returned a non-finite or out-of-range rating")
    return result

def _weighted_score(breakdown, event_class='', tightness=None, payoff_valid=True):
    from scoring_policy import final_score
    return final_score(breakdown, event_class=event_class, tightness=tightness, payoff_valid=payoff_valid)

def _extra_editor_fields(data):
    from scoring_policy import EVENT_CLASSES
    event_class = str(data.get("event_class", "QUOTE")).upper()
    if event_class not in EVENT_CLASSES:
        event_class = "QUOTE"
    try:
        tightness = max(0.0, min(100.0, float(data.get("tightness", 70))))
    except (TypeError, ValueError):
        tightness = 70.0
    payoff_at = data.get("payoff_at_seconds")
    try:
        payoff_at = float(payoff_at) if payoff_at is not None else None
    except (TypeError, ValueError):
        payoff_at = None
    payoff_type = str(data.get("payoff_type", "none")).lower()
    if payoff_type not in {"line","reaction","physical","reveal","silence","consequence","none"}:
        payoff_type = "none"
    return event_class, tightness, payoff_at, payoff_type

# Circuit breaker: after several consecutive failures (e.g. a genuine DNS
# outage, not just one flaky request), stop hammering the API on every
# single window and fail fast instead -- retrying 3x with backoff on
# EVERY one of a hundred windows during a real outage wastes enormous
# amounts of time for zero benefit. Resets the moment a call succeeds.
class ProviderUnavailable(RuntimeError):
    pass

_fatal_provider_error = None
_consecutive_failures = 0
_CIRCUIT_BREAKER_THRESHOLD = 5
_circuit_open_until = 0.0
_CIRCUIT_COOLDOWN_SECONDS = 30
_rate_limit_lock = threading.Lock()
_next_llm_request_at = 0.0


def _reserve_openai_capacity(prompt: str) -> None:
    """Globally pace concurrent batches below the configured OpenAI TPM cap."""
    global _next_llm_request_at
    if Config.LLM_PROVIDER != "openai":
        return
    # A conservative tokenizer-independent estimate. Completion allowance is
    # included because TPM accounting covers input plus generated output.
    estimated_tokens = max(1000, len(prompt) // 4 + 2500)
    safe_tpm = max(1000, int(Config.OPENAI_TPM_LIMIT * 0.82))
    spacing = min(30.0, estimated_tokens * 60.0 / safe_tpm)
    with _rate_limit_lock:
        now = time.monotonic()
        slot = max(now, _next_llm_request_at)
        _next_llm_request_at = slot + spacing
    if slot > now:
        time.sleep(slot - now)


def _rate_limit_delay(resp, attempt: int) -> float:
    """Read provider guidance, supporting both seconds and milliseconds."""
    import re
    global _next_llm_request_at
    delay = 0.0
    try:
        delay = float(resp.headers.get("Retry-After", "0") or 0)
    except (TypeError, ValueError):
        pass
    match = re.search(r"try again in\s+([0-9.]+)\s*(ms|s)", resp.text, re.I)
    if match:
        parsed = float(match.group(1)) / (1000 if match.group(2).lower() == "ms" else 1)
        delay = max(delay, parsed)
    delay = min(60.0, max(delay + 0.75, 2 ** min(attempt, 5)))
    with _rate_limit_lock:
        _next_llm_request_at = max(_next_llm_request_at, time.monotonic() + delay)
    return delay


def _provider_for_model(model: str) -> str:
    """Route a model to its real provider; never use global LLM_PROVIDER for known models."""
    name = (model or "").strip().lower()
    if name.startswith("grok"):
        return "grok"
    if name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    provider = (Config.LLM_PROVIDER or "").strip().lower()
    if provider in ("openai", "grok"):
        return provider
    raise ProviderUnavailable(f"Cannot determine provider for model {model!r}")


def _api_key_for_provider(provider: str) -> str:
    """Use provider-specific credentials so xAI and OpenAI keys cannot cross over."""
    if provider == "openai":
        return (getattr(Config, "OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    return (getattr(Config, "XAI_API_KEY", "") or getattr(Config, "GROK_API_KEY", "") or
            os.getenv("XAI_API_KEY", "") or os.getenv("GROK_API_KEY", "")).strip()


def _extract_openai_response_text(payload: dict) -> str:
    """Extract text from an OpenAI Responses API payload without SDK dependency."""
    if isinstance(payload.get("output_text"), str) and payload["output_text"]:
        return payload["output_text"]
    chunks = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "".join(chunks)
    raise ValueError("OpenAI Responses API returned no text output")


def _call_llm(prompt: str, model: str, max_retries: int = 3) -> str:
    global _consecutive_failures, _circuit_open_until, _fatal_provider_error, _next_llm_request_at
    provider = _provider_for_model(model)
    api_key = _api_key_for_provider(provider)
    if not api_key:
        required = "OPENAI_API_KEY" if provider == "openai" else "XAI_API_KEY/GROK_API_KEY"
        raise ProviderUnavailable(f"{provider} model {model!r} requires {required}")
    if _fatal_provider_error:
        raise ProviderUnavailable(_fatal_provider_error)
    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD and time.time() < _circuit_open_until:
        raise RuntimeError(f"circuit breaker open ({_consecutive_failures} consecutive failures) -- skipping call, retry after cooldown")

    # GPT models use OpenAI's Responses API. Grok models use xAI's OpenAI-compatible chat endpoint.
    url = "https://api.openai.com/v1/responses" if provider == "openai" else GROK_URL
    # xAI can occasionally stall for a full read timeout.  Retrying three
    # times at 60 seconds per request made a completed export look hung and
    # delayed every later clip.  Keep OpenAI's rate-limit resilience, while
    # bounding Grok network failures to one short retry.
    attempts = max(max_retries, 7) if provider == "openai" else min(max_retries, 2)
    request_timeout = 60 if provider == "openai" else 25
    last_error = None
    for attempt in range(attempts):
        request_started = time.monotonic()
        try:
            if provider == "openai":
                estimated_tokens = max(1000, len(prompt) // 4 + 2500)
                safe_tpm = max(1000, int(Config.OPENAI_TPM_LIMIT * 0.82))
                spacing = min(30.0, estimated_tokens * 60.0 / safe_tpm)
                with _rate_limit_lock:
                    now = time.monotonic()
                    slot = max(now, _next_llm_request_at)
                    _next_llm_request_at = slot + spacing
                if slot > now:
                    time.sleep(slot - now)
            proxies = {"http": Config.NETWORK_PROXY, "https": Config.NETWORK_PROXY} if Config.NETWORK_PROXY else None
            body = ({"model": model, "input": prompt} if provider == "openai" else
                    {"model": model, "messages": [{"role": "user", "content": prompt}],
                     "temperature": 0.2, "max_tokens": 1024})
            resp = requests.post(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 json=body, timeout=request_timeout, proxies=proxies)
            if resp.status_code in (401, 402, 403):
                body_text = resp.text.lower()
                if 'missing_scope' in body_text or 'missing scopes' in body_text:
                    category = 'API key is missing model request permission'
                elif any(k in body_text for k in ('credit', 'spending', 'quota')):
                    category = 'provider credits or spending limit'
                else:
                    category = 'provider authentication or permission'
                _fatal_provider_error = f"{provider} HTTP {resp.status_code}: {category}. Processing stopped; fix provider settings before restarting."
                raise ProviderUnavailable(_fatal_provider_error)
            if resp.status_code == 429:
                from pipeline_metrics import api_call, retry
                api_call(kind="llm", provider=provider, model=model, elapsed=time.monotonic()-request_started, ok=False)
                if attempt < attempts - 1:
                    retry("llm_rate_limit")
                    wait = _rate_limit_delay(resp, attempt)
                    print(f"[detect] {provider} rate limit reached; preserving this batch and retrying in {wait:.1f}s ({attempt+1}/{attempts})", flush=True)
                    time.sleep(wait); continue
                raise requests.exceptions.HTTPError(f"429 rate limit remained after {attempts} attempts: {resp.text[:300]}")
            if not resp.ok:
                from pipeline_metrics import api_call
                api_call(kind="llm", provider=provider, model=model, elapsed=time.monotonic()-request_started, ok=False)
                raise requests.exceptions.HTTPError(f"{resp.status_code} error from {provider} API using model {model}: {resp.text[:300]}")
            _consecutive_failures = 0
            payload = resp.json()
            from pipeline_metrics import api_call
            api_call(kind="llm", provider=provider, model=model, elapsed=time.monotonic()-request_started,
                     usage=payload.get("usage") or {}, ok=True)
            if provider == "openai":
                return _extract_openai_response_text(payload)
            content = payload["choices"][0]["message"].get("content") or ""
            if not content.strip():
                raise ValueError(f"{provider} returned an empty assistant message")
            return content
        except requests.exceptions.ProxyError as e:
            raise RuntimeError('ProxyError: the configured network proxy is unreachable.') from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < attempts - 1:
                from pipeline_metrics import retry
                retry("llm"); wait = 2 ** attempt
                print(f"[detect] {provider} {type(e).__name__} on attempt {attempt+1}/{attempts}, retrying in {wait}s", flush=True)
                time.sleep(wait); continue
    _consecutive_failures += 1
    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until = time.time() + _CIRCUIT_COOLDOWN_SECONDS
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{provider} request failed without a response")


def _scoring_model() -> str:
    # Scout + primary/deep scoring are always OpenAI GPT-5.4-mini.
    return Config.OPENAI_SCORING_MODEL or "gpt-5.4-mini"


def _title_model() -> str:
    # Final title/post generation uses Grok 4.6. _call_llm routes it to xAI.
    return Config.GROK_TITLE_MODEL or "grok-4.6"


MIN_WORDS_TO_SCORE = 12
# Windows at or above this length always reach the LLM regardless of
# keyword signals -- protects recall against real judgment material the
# lexicon can't catch (sarcasm, implied hypocrisy, a slow-building
# story with no single "trigger word"). Only windows BELOW this length
# get the stricter keyword-based skip, since a short window with
# neither an action verb nor any judgment vocabulary is overwhelmingly
# likely to be genuine filler -- skipping those is real, free API-cost
# and wall-clock savings with very little recall risk.
KEYWORD_SKIP_MAX_WORDS = 35
DROPPED = None


def _should_skip_window(text):
    return not text.strip()


def _fold_reactions_into_reason(reason: str, predicted_reactions) -> str:
    """
    predicted_reactions exists mainly to force the model to ground its
    scores in concrete audience reactions before committing to numbers
    (see DETECTION_PROMPT) -- but a couple of them are also genuinely
    useful to surface in the UI's existing "why this clip" text, so
    they ride along on the `reason` field instead of needing a new
    field threaded through Candidate/score.json/every template.
    """
    if not isinstance(predicted_reactions, list) or not predicted_reactions:
        return reason
    sample = [str(r) for r in predicted_reactions[:3] if str(r).strip()]
    if not sample:
        return reason
    return f"{reason} (predicted reactions: {'; '.join(sample)})" if reason else f"predicted reactions: {'; '.join(sample)}"


def score_window(transcript_text: str):
    if _should_skip_window(transcript_text):
        return DetectionResult(score=0.0, reason="skipped_trivial_or_name_list")

    streamer_name = Config.STREAMER_NAME or "(unknown)"
    priority_names = Config.PRIORITY_NAMES or "(none configured)"
    prompt = DETECTION_PROMPT.format(
        transcript=transcript_text, streamer_name=streamer_name, priority_names=priority_names,
    )

    try:
        raw = _call_llm(prompt, model=_scoring_model())
    except ProviderUnavailable:
        raise
    except Exception as e:
        print(f"[detect] API call failed, dropping this window ({e})")
        return DROPPED

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        data = json.loads(cleaned)
        # Older provider responses called the subject/action signal
        # ``novelty``. Preserve those responses while new prompts emit the
        # explicit ``subject`` field.
        if 'subject' not in data and 'novelty' in data:
            data['subject'] = data['novelty']
        breakdown = {cat: float(data[cat]) for cat in CATEGORY_WEIGHTS}
        breakdown = _apply_hard_caps(breakdown)
        quote = data.get("quote", "")
        if quote and quote.lower() not in transcript_text.lower():
            quote = ""
        return DetectionResult(
            score=_weighted_score(breakdown, *_extra_editor_fields(data)[:2]),
            breakdown=breakdown,
            reason=_fold_reactions_into_reason(data.get("reason", ""), data.get("predicted_reactions")),
            safety_flag=data.get("safety_flag"),
            topic=data.get("topic", ""),
            quote=quote,
            judgment_target=str(data.get("judgment_target", "")).upper(),
            event_class=_extra_editor_fields(data)[0],
            tightness=_extra_editor_fields(data)[1],
            payoff_at_seconds=_extra_editor_fields(data)[2],
            payoff_type=_extra_editor_fields(data)[3],
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        print(f"[detect] ERROR invalid scoring response: {exc}")
        return DROPPED


BATCH_DETECTION_PROMPT = """You score MULTIPLE Kick/IRL transcript windows in one pass, for ONE \
audience: internet-native Kick-clips viewers who respond strongly to roastable behavior, debates, \
funny/awkward interactions, chaos, emotional reactions, bold takes, flex/status moments, reveals, \
and surprising streamer behavior. Not manufacturing \
controversy -- finding GENUINE footage where someone's own words/actions naturally give this \
audience a real reason to say something. The judgment target is NOT always the streamer -- \
identify who it actually is per window.

STREAMER is {streamer_name}. KNOWN_NAMES below are spellings only -- never raise a score \
just because a name was spoken.

KNOWN_NAMES (spelling reference ONLY): {priority_names}

For EACH window: identify who is involved and who the PRIMARY_JUDGMENT_TARGET actually is \
(judgment_target: STREAMER, SIDE_CHARACTER, BOTH, or OTHER -- don't default to STREAMER just \
because they're the one streaming). Then ask: "if this exact clip was posted to X with no \
misleading caption, what would this audience say about that target -- and would they say it \
WITHOUT needing a caption to tell them what to think?" Not "is this viral/controversial/\
funny." FIRST silently predict 5-10 short, casual hypothetical X replies about the target \
(predicted_reactions), THEN score based on what you actually predicted -- generic \
"lol"/"W"/"L" energy with no real judgment means both judgment and comment_potential score low.

Score 1-10 on:
- judgment (co-equal MOST IMPORTANT with comment_potential, and a HARD GATE): does the target \
hand this audience real evidence across any of: ego/arrogance/dominance, cringe/awkward/\
trying-too-hard, confidently-wrong stupidity, delusion (words contradicted by what's shown, \
zero self-awareness), entitlement, hypocrisy (says one thing, does another), disrespect, \
money/status flexing or gambling, social drama (rejection/fallout/betrayal), a callout \
(attacking/questioning another creator), or a controversial take where the TARGET becomes the \
object of judgment for how/what they said (not just the topic being heavy)? Much stronger if \
it contradicts the target's own established persona (e.g. "I don't care" then visibly furious \
at criticism). Score 1-3, ALWAYS, if: the target is merely reacting to someone else's action, \
only mentioned (not shown acting), someone else is the actually judgeable party, the \
"controversy" only exists via a caption, understanding needs outside context, the target \
isn't clearly responsible, or it's generic funny/gameplay/wholesome/motivational content with \
nothing to judge. GATE: a great hook or clean audio must never rescue weak judgment.
- comment_potential (co-equal MOST IMPORTANT with judgment, and a HARD GATE): would this \
audience actually reply with something specific, not scroll past? Count DISTINCT valid \
angles (arrogant AND stupid AND delusional together scores much higher than one weak angle); \
weigh roastability (an obvious thing to mock), debate potential, whether replies could turn \
into an argument, and comment ease (instantly obvious what to say). Only count reactions that \
are real judgment (cringe, arrogant, delusional, ego insane, needs humbling, entitled, \
hypocrite, he's mad because he's wrong) -- never count empty "lol"/"W"/"L"/"bro" noise. GATE: \
weak comment potential must score 1-3 regardless of anything else in the window.
- payoff: does the judgment moment actually CLOSE (ego->humbled, brag->failure, money \
flex->loss, claim->proven wrong, disrespect->consequence, hypocrisy->exposed, lie->caught, \
cringe->awkward reaction, flirting->rejection, callout->confrontation)? A setup that pays off \
outside this window is NOT a payoff.
- standalone: understandable with zero prior context, no explanation needed.
- hook: does the first 1-3s create curiosity around the behavior specifically (not a generic \
catchy opening with no judgment value underneath).
- novelty: specific/unexpected enough to stand out from routine streamer filler.
- emotion: real tension, shock, laughter, embarrassment, anger, warmth, excitement, fear, disbelief, awkwardness or sincerity.
- technical: clear audio/video, easy to follow -- lowest priority category.

Also flag safety concerns (minor_present, explicit_content, unverified_serious_claim, or \
null), and return a topic label + a quote (exact substring of THAT window's transcript, or \
empty) for each.

WINDOWS:
{windows_block}

Respond with ONLY a JSON array, exactly one object per window, each with an "index" field:
[
  {{"index": 0, "judgment_target": "STREAMER|SIDE_CHARACTER|BOTH|OTHER", \
"predicted_reactions": ["<short reaction>", "..."], "judgment": <1-10>, \
"comment_potential": <1-10>, "payoff": <1-10>, "standalone": <1-10>, "hook": <1-10>, \
"novelty": <1-10>, "emotion": <1-10>, "technical": <1-10>, "safety_flag": <string or null>, "topic": "<short label>", \
"quote": "<exact substring or empty>", "reason": "<one short sentence, no banned names unless they appear in this window>"}},
  ...
]
"""


def score_batch(window_texts: list[str], model=None):
    """
    Scores several windows in ONE API call instead of one call per
    window -- keeps the full category breakdown per window; just stops
    paying for a separate HTTP round-trip every 20-75 seconds of
    content.
    """
    if not window_texts:
        return []

    results: list = [None] * len(window_texts)
    to_send = []
    for i, text in enumerate(window_texts):
        if _should_skip_window(text):
            results[i] = DetectionResult(score=0.0, reason="skipped_trivial_or_name_list")
        else:
            to_send.append((i, text))

    if not to_send:
        return results

    streamer_name = Config.STREAMER_NAME or "(unknown)"
    priority_names = Config.PRIORITY_NAMES or "(none configured)"
    windows_block = "\n\n".join(
        f'--- window {batch_idx} ---\n"""{text}"""' for batch_idx, (_, text) in enumerate(to_send)
    )
    prompt = BATCH_DETECTION_PROMPT.format(
        streamer_name=streamer_name, priority_names=priority_names, windows_block=windows_block,
    )

    try:
        raw = _call_llm(prompt, model=model or _scoring_model())
    except ProviderUnavailable:
        raise
    except Exception as e:
        print(f"[detect] batch API call failed, dropping all {len(to_send)} sent windows in this batch ({e})")
        for orig_i, _ in to_send:
            results[orig_i] = DROPPED
        return results

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        data = json.loads(cleaned)
        by_batch_index = {int(item["index"]): item for item in data}
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        print(f"[detect] batch response could not be parsed, dropping all {len(to_send)} sent windows in this batch")
        for orig_i, _ in to_send:
            results[orig_i] = DROPPED
        return results

    for batch_idx, (orig_i, text) in enumerate(to_send):
        item = by_batch_index.get(batch_idx)
        if item is None:
            print(f"[detect] ERROR missing batch result {batch_idx}")
            results[orig_i] = DROPPED
            continue
        try:
            if 'subject' not in item and 'novelty' in item:
                item['subject'] = item['novelty']
            breakdown = {cat: float(item[cat]) for cat in CATEGORY_WEIGHTS}
            breakdown = _apply_hard_caps(breakdown)
            quote = item.get("quote", "")
            if quote and quote.lower() not in text.lower():
                quote = ""
            results[orig_i] = DetectionResult(
                score=_weighted_score(breakdown, *_extra_editor_fields(item)[:2]),
                breakdown=breakdown,
                reason=_fold_reactions_into_reason(item.get("reason", ""), item.get("predicted_reactions")),
                safety_flag=item.get("safety_flag"),
                topic=item.get("topic", ""),
                quote=quote,
                judgment_target=str(item.get("judgment_target", "")).upper(),
                event_class=_extra_editor_fields(item)[0],
                tightness=_extra_editor_fields(item)[1],
                payoff_at_seconds=_extra_editor_fields(item)[2],
                payoff_type=_extra_editor_fields(item)[3],
            )
        except (KeyError, ValueError, TypeError):
            results[orig_i] = DROPPED

    return results


RANK_TOP_MOMENTS_PROMPT = """You are picking which clips from a Kick VOD should actually get \
posted today, out of the moments that already survived scoring and merging below. The \
system's objective is finding clips that make the REPLIES the second half of the content -- \
someone in the clip (not always the streamer) getting roasted, argued about, or judged by an \
edgy X audience -- ego, cringe, hypocrisy, delusion, callouts, humbling moments -- not clips \
that are merely entertaining or catchy.

CANDIDATES (already scored and merged, listed as index, time, score, topic, quote):
{candidates_block}

Pick the 3-8 BEST candidates that should actually post today, by index only. Prioritize \
judgment + comment potential + payoff strength (would this genuinely get roasted/argued \
about, unprompted?) over a merely \
catchy hook. Do not invent new times or content -- choose only from the indices given. If \
fewer than 3 are genuinely worth posting, return \
fewer -- do not pad with weak ones just to hit a number.

Respond with ONLY a JSON array of the chosen indices, e.g. [0, 3, 5]. No other text.
"""


def rank_top_moments(candidates: list) -> list[int]:
    """
    A second, whole-video pass over everything that already survived
    scoring + merging -- picks the best 3-8 to actually post, the way a
    human editor would look at a shortlist rather than judging each
    moment in total isolation.
    """
    if len(candidates) <= 3:
        return list(range(len(candidates)))

    lines = []
    for i, c in enumerate(candidates):
        quote = getattr(c, "quote", "") or "(none)"
        lines.append(
            f"{i}: {c.start:.0f}s-{c.end:.0f}s score={c.score} "
            f"topic={c.topic!r} class={getattr(c, 'moment_type', '')!r} tightness={getattr(c, 'tightness', None)!r} quote={quote!r} evidence_and_critique={c.reason!r}"
        )
    prompt = RANK_TOP_MOMENTS_PROMPT.format(candidates_block="\n".join(lines))

    try:
        raw = _call_llm(prompt, model=_scoring_model())
    except Exception as e:
        print(f"[detect] top-moments ranking call failed ({e}) -- skipping the ranking pass, "
              f"keeping every candidate that already cleared its own bar")
        return list(range(len(candidates)))

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        indices = json.loads(cleaned)
        return [i for i in indices if isinstance(i, int) and 0 <= i < len(candidates)]
    except (json.JSONDecodeError, TypeError):
        print("[detect] top-moments ranking response could not be parsed -- keeping every "
              "candidate that already cleared its own bar")
        return list(range(len(candidates)))

# Current cross-platform rubric supersedes the historical X-only prompt above.
from scoring_policy import WEIGHTS, SINGLE, BATCH, RANK
CATEGORY_WEIGHTS = WEIGHTS
DETECTION_PROMPT = SINGLE
BATCH_DETECTION_PROMPT = BATCH
RANK_TOP_MOMENTS_PROMPT = RANK


_uncached_call_llm = _call_llm

def _call_llm(prompt, model, max_retries=3):
    """Reuse valid structured analysis within the source-scoped job cache."""
    import os
    from pathlib import Path
    from pipeline_state import fingerprint, check_cancelled
    check_cancelled()
    directory = os.getenv('JOB_ANALYSIS_CACHE_DIR')
    path = Path(directory)/(fingerprint({'version':2,'prompt':prompt,'model':model})+'.json') if directory else None
    if path and path.exists():
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(saved.get('response'),str):
                from pipeline_metrics import cache_hit
                cache_hit('llm')
                return saved['response']
        except (ValueError,TypeError): pass
    raw = _uncached_call_llm(prompt,model,max_retries)
    if path:
        try:
            parsed = json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
            if isinstance(parsed,(dict,list)):
                from pipeline_state import atomic_json
                atomic_json(path,{'response':raw})
        except (ValueError,TypeError): pass
    return raw

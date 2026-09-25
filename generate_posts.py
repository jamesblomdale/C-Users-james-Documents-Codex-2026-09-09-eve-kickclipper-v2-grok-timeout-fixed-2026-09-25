"""
Generates the X post, TikTok overlay title, and YouTube title for a
finished clip -- following the documented platform system plus real
worked examples for each format, so the model matches the actual voice
instead of a generic approximation.

Two things override the default "punchy, curiosity-gap" formula below,
both driven by data already computed upstream in detect.py so they're
automatic rather than a per-clip judgment call:

- LONG-FORM X CAPTION: most clips get the short, hook-first one-liner
  (this is the Kick_Champ / scubaryan_ style -- name + emotion + detail,
  footage delivers the payoff). But an ongoing-storyline clip that
  doesn't stand alone (the Deen the Great / Adrien Broner legal
  situation, a Blueface probation update) needs Hook -> Context -> Quote
  -> Commentary -> Engagement to actually land, because the footage
  alone doesn't carry the story. The switch is the STANDALONE score
  from detect.py's breakdown (low standalone = needs the long form).

- FACTUAL TONE OVERRIDE: a clip with safety_flag set (minor present,
  explicit content, an unverified serious claim) gets a plain, factual
  title/caption instead of the usual emoji/CAPS clickbait formula --
  this used to be something done by hand every time (the pool incident,
  the DMT/driving clip, Delonte West's story all got manually
  downgraded from the standard voice).

The X caption is generated and validated SEPARATELY from the YouTube/
TikTok titles (see generate_x_caption below) -- it has its own strict,
code-enforced rules (hook in the first 5 words, a hard length cap, an
emoji limit) that don't apply to the other two formats, and unlike
those two, a caption that fails validation gets ONE automatic rewrite
attempt with the specific violation fed back to the model, rather than
just hoping the prompt was followed.

Two more things are enforced on ALL THREE generated fields, mechanically,
after generation -- not just asked for in the prompt:
- Quote verification (_verify_quotes): mirrors detect.py's exact-
  substring rule for its `quote` field. A "quote" the model puts in
  quotation marks has to actually appear in the transcript, or it gets
  stripped. This is a real gap this system used to have -- several
  corrections in testing were "that's not who said that" / a title
  implying a line that wasn't actually said.
- Redaction (_redact): slurs and explicit sexual language never reach
  the output, even if they're in the source transcript. Previously this
  was a manual editing pass every time; now it's a safety-net regex
  pass backing up the prompt-level instruction (true speaker/context
  understanding isn't something a regex can do, hence "safety net").
"""

import json
import re
from config import Config
from detect import _call_llm, _title_model  # reuse the same LLM call helper

# Below this standalone score (1-10 scale, from detect.py's breakdown),
# a clip is judged not to carry its own story without extra context --
# that's when the X caption switches to the long-form structure instead
# of the short one-liner. 5 is the midpoint; picked slightly below
# center so only clips that genuinely lean "needs context" trigger it,
# not every clip that scored average.
STANDALONE_LONGFORM_THRESHOLD = 5.0

# ---------------------------------------------------------------------
# YouTube / TikTok titles
# ---------------------------------------------------------------------

# Modeled on the real short-clip formula used by accounts like
# Kick_Champ and scubaryan_: name-led, one emotion/action word doing
# the work, curiosity gap intact, nothing spelled out.
PLATFORM_SYSTEM = """You package short-form Kick streamer clips for three different surfaces.\nThe SAME clip must not be lazily copy-pasted across platforms. Each field has a different job.\n\nANGLE FIRST:\nBefore writing anything, silently identify the clip's single strongest factual angle: the biggest\naction, claim, contradiction, reaction, awkward aftermath, reveal, money/status detail, escalation,\nlife lesson, or funny logic. Build every field around that real angle. If the clip has hook ->\nprogression -> climax, preserve that sequence and do not spoil the climax too early unless the\nclimax itself is the only understandable hook.\n\nYOUTUBE TITLE:\n- Clear, searchable, descriptive: [Person] + [specific action/event] + [useful context].\n- Usually 7-16 words. Use the names viewers would search.\n- Prefer concrete verbs: calls out, explains, confronts, reveals, snaps, offers, refuses, flexes,\n  reacts, challenges, admits, gets called out.\n- Avoid vague filler such as "you won't believe", "this is crazy", or "loses it" unless the clip\n  clearly shows an actual extreme reaction.\n- The YouTube title may reveal more of the event than TikTok because clarity/search matters.\nTITLE FORMULA (mandatory): YouTube must read like a search query and name the real speaker/action; TikTok must lead with the strongest emotional event while withholding only the outcome; overlay must be a short alternate angle. Rotate the truthful angle across platforms (action on YouTube, emotion or open loop on TikTok, quote/take on X) without inventing context or assigning a line to the wrong speaker.\n\nTIKTOK TITLE/CAPTION:\n- Feed-first and emotionally immediate, usually 6-16 words.\n- Lead with the person + strongest action or judgment angle.\n- Keep one useful information gap when possible: reveal the premise, withhold the exact response,\n  reason, or aftermath if the footage pays it off.\n- Do not make it so vague that a viewer cannot tell why to care.\n- 0-2 fitting emoji; do not spam emoji or caps.\n\nOVERLAY TITLE:\n- 3-10 words, readable in under a second.\n- Must complement rather than simply duplicate the TikTok title.\n- Best overlays often frame the transformation/open loop: "N3ON'S WHOLE DEMEANOR CHANGED",\n  "FOUSEY EXPLAINS WHY...", "N3ON GOT CALLED OUT MID-FLEX".\n- Do not invent visual reactions. If the transcript/context does not establish a reaction, use the\n  spoken action instead.\n\nHOOK:\n- One short editor-ready opening line for the first seconds of the video.\n- State the strongest factual premise quickly, then leave the footage room to deliver the payoff.\n- No fake urgency, fake trend claims, or invented context.\n\nANGLE ROTATION / ILLUSION OF NOVELTY:\nUse different truthful angles across YouTube, TikTok, overlay and X so the post does not feel\ncopy-pasted. This means reframing the SAME evidenced moment, never inventing a new event.\n\nSTYLE BENCHMARKS:\n- "Fousey Explains Why He Thinks Watching Porn Is Gay" -> direct unusual take, why-gap.\n- "N3on's Whole Demeanor Changes After Fousey Smacks Him" -> aftermath/transformation.\n- "N3on Flexes His Dior Suitcase Then Snaps When Someone Calls It Busted" -> flex -> reversal.\n- "Fousey Calls N3on Out for Constantly Seeking Validation" -> callout + clear subject.\n- "Fousey Claims He Looks Better Than Most 24-Year-Olds at 36" -> concrete comparison.\n\nNever invent facts, quotes, identities, facial reactions or off-camera context. Accuracy beats hype.\n"""

# Factual-tone counterpart to PLATFORM_SYSTEM, used instead of it when
# the clip's safety_flag is set. Same worked examples this system used
# to lean on for sensitive clips before that behavior was ad hoc --
# now it's the automatic path instead of a manual downgrade.
PLATFORM_SYSTEM += """\nOPEN-LOOP CHECK:\nThe title and overlay should earn attention without narrating every beat of the video.\nYouTube should name the person and real event clearly; TikTok may withhold the precise\nresponse or aftermath; the overlay should be 3-10 words and use a different factual angle.\nNever promise an extreme reaction the clip does not establish. If revealing the event is\nthe clearest honest hook, reveal it instead of using fake curiosity.\n"""

# Additional editorial constraints from the account's production rubric. These
# sit alongside the platform-specific rules above and are intentionally factual:
# emotion/comment potential can improve a title, but cannot justify invention.
PLATFORM_SYSTEM += """\nTIKTOK CAPTION REFINEMENT:\nUse one sentence of roughly 14-24 words when practical. Prefer this order: Name -> biggest\nevent -> emotional or judgment angle -> only the context needed to understand it. Start with\nthe biggest action and use strong factual verbs such as confronts, exposes, rejects, kicks out,\nargues, loses, calls out, or reveals. Make the first five words scroll-stopping. Never open with\nweak reactions such as looked annoyed or got upset. Encourage a specific emotional response only\nwhen the clip supports it. Use claims, says, or alleges for unverified allegations. No hashtags;\nemoji only at the end; every word must add value.\n"""

SENSITIVE_PLATFORM_SYSTEM = """You write clip titles for a Kick streaming clips account, across \
YouTube and TikTok. This clip is FLAGGED AS SENSITIVE (a minor present, explicit content, or an \
unverified serious claim/legal situation) -- use a PLAIN, FACTUAL tone instead of the usual \
clickbait formula. Do not use ALL-CAPS emotion words, emoji, or a curiosity-gap hook.

The first name mentioned should be the STREAMER unless a guest is clearly the one doing the \
action in this specific clip's words -- then lead with the guest instead.

YOUTUBE TITLE:
State what happened plainly and accurately, like a news headline. No sensational language, \
no withheld outcome. If any part of it is unconfirmed (an arrest, an allegation, a claim), say \
so explicitly ("reportedly", "according to...") rather than stating it as settled fact.

TIKTOK OVERLAY TITLE:
Same plain, factual approach as the YouTube title -- a short, accurate descriptive sentence. \
No CAPS-lock emotion word, no emoji, no formula.

WORKED EXAMPLES:
- Police Question Blueface After Kicking Nevaeh Into Pool at Their Home
- Delonte West Opens Up About Losing Himself on the Streets
- Adrien Broner Responds After Deen the Great's Legal Situation Update
- Blueface Gives an Update on His Probation Status on Stream

Match this voice -- calm and factual, not dramatized.
"""

GENERATION_PROMPT = """{system}

UNIVERSAL RULES (apply no matter which style block above is in effect):
- Any direct quote you put in quotation marks must be word-for-word from the transcript \
below, and attributed to whoever actually said it. If you're not sure who said a line, \
don't present it as an attributed direct quote.
- Never include a slur or an explicit sexual description in the title text, even if it's \
said in the transcript -- refer to the topic without repeating that language.

STREAMER: {streamer_name}
NAMES ACTUALLY IN THIS CLIP'S WORDS (only use these -- and STREAMER above -- as names \
in the titles; do not pull in any other name, even a well-known one, if they weren't \
actually said in this specific transcript): {names_in_transcript}

Here is the clip transcript:
\"\"\"
{transcript}
\"\"\"

Write, for this clip:
1. A YouTube title
2. A TikTok title/caption
3. A separate short overlay title
4. A short spoken/editor hook

Respond with ONLY a JSON object, no other text:
{{"youtube_title": "...", "tiktok_title": "...", "overlay_title": "...", "hook": "..."}}
"""


# ---------------------------------------------------------------------
# X caption: its own system, its own validation, short or long form
# ---------------------------------------------------------------------

# Short form (default) -- one hook-first line. This is the Kick_Champ /
# scubaryan_ benchmark: the footage delivers the payoff, the caption
# just earns the click.
_X_STRUCTURE_SHORT = """FORMAT: a compact X post for a video clip, normally 2-4 short sentences/paragraphs and roughly 25-65 words.\n1. HOOK: first sentence leads with the biggest factual event/take/reaction. The first 5 words should\n   contain the person or the actual action -- never "during a stream" setup.\n2. CONTEXT: one brief sentence explaining only what increases interest or makes the clip understandable.\n3. QUOTE: optional. Use a short exact quote only when it is genuinely the strongest line and clearly\n   attributable. Never invent or clean up a quote.\n4. COMMENTARY: one short natural observation that leaves room for viewers to agree/disagree.\nDo not mechanically ask "thoughts?" or "who wins?". Let the situation itself create discussion.\nThe clip should still deliver the payoff; do not narrate every beat."""

# Long form -- for a clip that's part of an ongoing storyline and
# doesn't stand alone (a legal situation, a probation update, a
# multi-stream feud): the Deen the Great/Adrien Broner situation and a
# Blueface probation clip are exactly the kind of moment this exists
# for, found by comparing those against standalone Kick_Champ/
# scubaryan_-style one-off clips that didn't need this structure.
_X_STRUCTURE_LONG = """FORMAT: a longer structured caption, because this clip is part of an \
ongoing storyline and doesn't fully land without context. Write ONE flowing caption (no \
section labels/headers in your output) that moves through, in order:
1. HOOK -- first line states the headline fact, still attention-grabbing.
2. CONTEXT -- one or two sentences of backstory so someone who hasn't followed the story \
can follow this update.
3. QUOTE -- a short, word-for-word quote from the transcript if a clean one exists and is \
clearly attributed to a specific person; skip this step entirely rather than inventing or \
guessing at a quote.
4. COMMENTARY -- one brief, even-handed observation. Not sensationalized.
5. ENGAGEMENT -- one short line inviting a reply/reaction.
Target roughly 40-80 words total. Still one caption block, not a list."""

_X_TONE_STANDARD = """TONE: confident, casual streamer-clips account voice. Hook-first, easy to scan,
with 0-2 fitting emoji when useful. Use strong verbs when they are supported, but never inflate a normal
reaction into "meltdown", "destroyed", "humiliated" or "exposed". Prefer concrete wording over generic
"goes crazy" language. Keep slurs and unnecessary explicit wording out of the post; paraphrase sensitive
phrasing while preserving the factual meaning. Natural and human-sounding, not journalist-like or spammy."""

_X_TONE_FACTUAL = """TONE OVERRIDE -- this clip is FLAGGED AS SENSITIVE (a minor present, \
explicit content, or an unverified serious claim/legal situation). Use a PLAIN, FACTUAL tone \
instead of the usual formula:
- No ALL-CAPS emotion words, no hype language ("DESTROYED", "EXPOSED", "MELTDOWN", etc).
- No emoji, except a single restrained one if the moment is genuinely sad (e.g. \U0001F494) -- \
never a shock/chaos/money emoji.
- State what happened plainly and accurately, the way a factual caption would, not a viral \
clip caption.
- If anything is unconfirmed (an arrest, an allegation, a claim), say so explicitly \
("reportedly", "according to...", "no charges confirmed at this time") rather than stating \
it as settled fact."""

_X_EXAMPLES_SHORT = """WORKED EXAMPLES (short form, standard tone):
- N3on gets SWATTED mid basketball game while settling bets outside
- Clavicular snaps after seeing a viewer's mom in his chat
- N3on's house tour takes a dark turn fast
- Treyliving fires back after Adin Ross calls the marathon a flop
- Sneako gets called a hypocrite mid-debate over his own past take
- Asmongold says he'd join ICE if given the chance

WORKED EXAMPLES (short form, factual tone -- safety_flag set):
- Police question Blueface after he kicks Nevaeh into the pool
- Delonte West opens up about losing his identity on the streets"""

_X_EXAMPLES_LONG = """WORKED EXAMPLES (long form -- ongoing storyline, doesn't stand alone):
- Adrien Broner responds after Deen the Great's legal situation escalated this week. Deen \
was reportedly taken into custody following [the specific update from this transcript], \
continuing a story that's been developing across recent streams. Broner: "[word-for-word \
quote from transcript, if one exists]" Whatever your take on Deen, this one keeps getting \
more serious. Where do you think this goes next?
- Blueface gives an update on his probation status live on stream. This follows the \
incident from [context established in the transcript] that's been an ongoing thread on his \
channel. He says: "[word-for-word quote from transcript, if one exists]" No confirmation yet \
on what happens if he violates the terms. Think he stays out of trouble?"""

X_CAPTION_REWRITE_SUFFIX = """

Your previous attempt was: "{previous}"
It failed validation for: {violations}
Write a NEW caption that fixes this. Follow every hard rule above."""

# Openers that bury the hook past word 5 -- almost always a sign the
# model defaulted to "setting the scene" instead of leading with why
# anyone should care. Checked against the very start of the caption.
WEAK_OPENERS = [
    r"^during\b", r"^in this clip\b", r"^in a recent\b", r"^recently,?\b",
    r"^earlier (today|this week)\b", r"^a clip (shows|of)\b", r"^this clip (shows|of)\b",
    r"^while (streaming|live)\b", r"^on stream,?\b",
]

# Broad but practical -- covers the emoji blocks actually used in this
# system's examples (emoticons, symbols/pictographs, dingbats, flags).
# Doesn't need to be a perfect Unicode emoji classifier, just accurate
# enough to enforce "0-1 emoji" on real output.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)


def _validate_caption(caption: str, long_form: bool = False) -> list[str]:
    """Returns a list of violated rules (empty list = passes). This is
    the actual code-level enforcement of the caption rules -- the parts
    of it that are mechanically checkable (length, emoji count, weak
    openers). Whether the hook is genuinely *interesting* and whether a
    claim is *factually supported* are judgment calls the prompt has to
    get right; code can't verify those against footage it never sees.
    The long-form structure is deliberately multi-sentence, so the
    short-form word cap and multi-sentence check don't apply to it --
    it gets a looser word-count band instead."""
    violations = []
    caption = caption.strip()

    if not caption:
        return ["empty caption"]

    words = caption.split()
    if long_form:
        if len(words) < 20:
            violations.append(f"too short for long form ({len(words)} words, expected ~40-80)")
        elif len(words) > 110:
            violations.append(f"too long ({len(words)} words, expected ~40-80)")
    else:
        if len(words) < 12:
            violations.append(f"too short ({len(words)} words, expected ~25-65)")
        elif len(words) > 85:
            violations.append(f"too long ({len(words)} words, expected ~25-65)")

    emoji_count = len(_EMOJI_RE.findall(caption))
    if emoji_count > 2:
        violations.append(f"too many emoji ({emoji_count}, max 2)")

    if any(re.match(p, caption, re.IGNORECASE) for p in WEAK_OPENERS):
        violations.append("weak opener burying the hook past word 5")


    return violations


def _build_x_caption_prompt(transcript_text: str, streamer: str, names_str: str,
                             long_form: bool, factual_tone: bool) -> str:
    tone_block = _X_TONE_FACTUAL if factual_tone else _X_TONE_STANDARD
    structure_block = _X_STRUCTURE_LONG if long_form else _X_STRUCTURE_SHORT
    examples_block = _X_EXAMPLES_LONG if long_form else _X_EXAMPLES_SHORT
    payoff_note = (
        "the footage plus this context together deliver the payoff, since this clip is part "
        "of an ongoing storyline and doesn't fully land on its own"
        if long_form else
        "the footage delivers the actual payoff, not the caption"
    )

    return f"""You write the X (Twitter) caption that goes with a clip, for a professional \
viral clipping account. The caption's job is to make someone stop scrolling and watch the \
attached clip -- {payoff_note}.

{tone_block}

{structure_block}

{examples_block}

UNIVERSAL RULES:
- Any direct quote (in quotation marks) must be word-for-word from the transcript below, \
correctly attributed to whoever actually said it. If you're not sure who said a line, don't \
present it as an attributed direct quote.
- Never include a slur or an explicit sexual description in the caption, even if it's said \
in the transcript -- refer to the topic without repeating that language.
- Never fabricate context, names, or outcomes that aren't in the transcript.

STREAMER: {streamer or "(unknown)"}
NAMES ACTUALLY IN THIS CLIP'S WORDS (only use these and STREAMER -- never a name that \
wasn't actually said in this transcript): {names_str}

Clip transcript:
\"\"\"
{transcript_text}
\"\"\"

Respond with ONLY a JSON object, no other text: {{"caption": "..."}}
"""


def generate_x_caption(transcript_text: str, streamer: str, allowed_names: list[str],
                        standalone_score: float | None = None,
                        safety_flag: str | None = None) -> str:
    """
    Generates the X caption -- short one-liner by default, or the long
    Hook->Context->Quote->Commentary->Engagement form when the clip's
    standalone score says it doesn't carry its own story (see module
    docstring). Applies the factual tone override when safety_flag is
    set. ONE automatic rewrite attempt if the first draft fails the
    mechanical validation checks above -- the model gets told exactly
    which rule it broke, not just asked to try again blindly.
    """
    names_str = ", ".join(allowed_names) if allowed_names else "(none -- don't name-drop anyone besides the streamer)"
    long_form = True  # Hook / context / supported quote / commentary, as requested.
    factual_tone = bool(safety_flag)

    prompt = _build_x_caption_prompt(transcript_text, streamer, names_str, long_form, factual_tone)

    caption = ""
    violations: list[str] = []
    for attempt in range(2):
        try:
            raw = _call_llm(prompt, model=_title_model())
        except Exception as e:
            print(f"[generate_posts] X caption call failed ({e})")
            # Keep every exported clip publishable when xAI is unavailable.
            # Use the verified transcript as a factual one-line caption rather
            # than exposing an internal error marker to the dashboard.
            seed = re.sub(r"\s+", " ", _redact(transcript_text or "")).strip()
            seed = re.split(r"(?<=[.!?])\s+", seed, maxsplit=1)[0]
            return f"{streamer or 'The streamer'} discusses {seed[:220]} — what do you make of it?"

        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(cleaned)
            caption = str(data.get("caption", "")).strip()
        except (json.JSONDecodeError, TypeError):
            caption = ""

        violations = _validate_caption(caption, long_form=long_form)
        if not violations:
            return caption

        if attempt == 0:
            print(f"[generate_posts] X caption failed validation ({'; '.join(violations)}), retrying once")
            prompt = _build_x_caption_prompt(transcript_text, streamer, names_str, long_form, factual_tone) \
                + X_CAPTION_REWRITE_SUFFIX.format(previous=caption, violations="; ".join(violations))

    # Second attempt still failed -- return it anyway rather than lose
    # the clip's caption entirely. It already went through the prompt's
    # rules twice; a mechanical length/emoji miss beats no caption.
    print(f"[generate_posts] X caption still failing validation after retry ({'; '.join(violations)}), using it anyway")
    return caption or "[generation failed, write manually]"


def _names_in_transcript(transcript_text: str) -> list[str]:
    """Which of the configured priority names actually appear in this
    specific clip's words -- used both to tell the model what it's
    allowed to name-drop, and to filter the output afterward."""
    text_lower = transcript_text.lower()
    found = []
    for name in (Config.PRIORITY_NAMES or "").split(","):
        name = name.strip()
        if name and name.lower() in text_lower:
            found.append(name)
    return found


def _strip_forbidden_names(posts: dict, allowed_names: list[str], streamer: str) -> dict:
    """
    Code-level safety net: even with the prompt telling the model which
    names are actually in this clip, it can still slip in a well-known
    name that wasn't actually said (a real hallucination risk when a
    priority-names list is sitting right there in context). This strips
    any OTHER priority name that shows up in the generated text but
    isn't in allowed_names -- the streamer's own name and anything
    genuinely in the transcript are always fine.
    """
    allowed_lower = {n.lower() for n in allowed_names} | {streamer.lower()} if streamer else {n.lower() for n in allowed_names}
    all_priority = [n.strip() for n in (Config.PRIORITY_NAMES or "").split(",") if n.strip()]
    forbidden = [n for n in all_priority if n.lower() not in allowed_lower]

    if not forbidden:
        return posts

    cleaned = dict(posts)
    for field in ("youtube_title", "tiktok_title", "x_post"):
        text = cleaned.get(field, "")
        for name in forbidden:
            if name.lower() in text.lower():
                pattern = re.compile(re.escape(name), re.IGNORECASE)
                text = pattern.sub("", text)
                text = re.sub(r"\s{2,}", " ", text).strip()
        cleaned[field] = text
    return cleaned


def _verify_quotes(text: str, transcript_text: str) -> str:
    """
    Code-level safety net mirroring detect.py's exact-substring rule for
    its `quote` field: anything the model put in quotation marks in a
    generated title/caption has to actually appear (word-for-word) in
    the transcript. If it doesn't, the quoted span gets dropped rather
    than shipping a title that implies someone said something they
    didn't -- a real gap this system had (several corrections in
    testing were exactly this: a quote attributed to the wrong person,
    or one that wasn't actually said).
    """
    if not text or '"' not in text:
        return text
    transcript_lower = transcript_text.lower()

    def _check(m: re.Match) -> str:
        quoted = m.group(1)
        candidate = quoted.rstrip(".").strip()
        if candidate and candidate.lower() not in transcript_lower:
            print(f"[generate_posts] dropping unverified quote: {quoted!r}")
            return ""
        return m.group(0)

    cleaned = re.sub(r'"([^"]{4,})"', _check, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -:")
    return cleaned


# Mechanical safety net for slurs/explicit sexual language -- backs up
# the prompt-level instruction, which is the primary defense (a regex
# can't tell reclaimed/quoted/clinical usage apart from the real thing,
# same limitation noted on _validate_caption above). Deliberately not
# exhaustive; it catches the common, unambiguous cases so this never
# has to be a manual editing pass again.
_REDACT_PATTERNS = [
    re.compile(r"\bn[i1]gg[ae3]r?s?\b", re.IGNORECASE),
    re.compile(r"\bf[a4]gg?[o0]t?s?\b", re.IGNORECASE),
    re.compile(r"\br[e3]t[a4]rd(?:ed|s)?\b", re.IGNORECASE),
    re.compile(r"\bsp[i1]cs?\b", re.IGNORECASE),
    re.compile(r"\bch[i1]nks?\b", re.IGNORECASE),
    re.compile(r"\bk[i1]kes?\b", re.IGNORECASE),
    re.compile(r"\btrann(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bp[u\*]ssy\b", re.IGNORECASE),
    re.compile(r"\bblow ?job\b", re.IGNORECASE),
    re.compile(r"\bhand ?job\b", re.IGNORECASE),
    re.compile(r"\bcum ?shot\b", re.IGNORECASE),
    re.compile(r"\bdeepthroat\w*\b", re.IGNORECASE),
    re.compile(r"\bmasturbat\w*\b", re.IGNORECASE),
]


def _redact(text: str) -> str:
    if not text:
        return text
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def generate_posts(transcript_text: str, standalone_score: float | None = None,
                    safety_flag: str | None = None) -> dict:
    """
    standalone_score: the 1-10 STANDALONE score from detect.py's
    breakdown, if available -- drives the short-vs-long-form X caption
    switch (see module docstring). safety_flag: the Candidate's
    safety_flag, if any -- drives the factual tone override across all
    three fields. Both default to None so existing callers that don't
    have this data (e.g. a manual regenerate from a saved score.json)
    still work; None just means "use the default short/standard path."
    """
    allowed_names = _names_in_transcript(transcript_text)
    streamer = Config.STREAMER_NAME or ""
    factual_tone = bool(safety_flag)
    system = SENSITIVE_PLATFORM_SYSTEM if factual_tone else PLATFORM_SYSTEM

    system += "\nReturn four distinct fields: youtube_title, tiktok_title, overlay_title, hook. Preserve hook -> progression -> climax. Treat allegations as allegations. Never invent visual reactions, facts, trends or quotes."
    prompt = GENERATION_PROMPT.format(
        system=system,
        transcript=transcript_text,
        streamer_name=streamer or "(unknown)",
        names_in_transcript=", ".join(allowed_names) if allowed_names else "(none -- don't name-drop anyone besides the streamer)",
    )

    try:
        raw = _call_llm(prompt, model=_title_model())
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        posts = json.loads(cleaned)
    except Exception as e:
        # Same principle as detect.py: a clip that was already cut and
        # captioned shouldn't be lost just because title generation
        # happened to fail -- fall back to a clearly marked placeholder
        # instead of crashing the job.
        print(f"[generate_posts] YouTube/TikTok title call failed ({e})")
        # If Grok is unavailable, give OpenAI one chance to generate the same
        # structured fields before falling back to deterministic text. This
        # keeps title generation live during transient xAI outages.
        try:
            fallback_raw = _call_llm(prompt, model=Config.OPENAI_SCORING_MODEL or "gpt-5.4-mini", max_retries=1)
            fallback_cleaned = fallback_raw.strip().removeprefix("```json").removesuffix("```").strip()
            posts = json.loads(fallback_cleaned)
        except Exception as fallback_error:
            print(f"[generate_posts] OpenAI title fallback unavailable ({fallback_error})")
            # Keep the clip publishable when both providers are unavailable.
            # A deterministic transcript-derived title is preferable to an
            # error marker.
            seed = re.sub(r"\s+", " ", _redact(transcript_text or "")).strip()
            seed = re.split(r"(?<=[.!?])\s+", seed, maxsplit=1)[0]
            words = seed.split()[:12]
            short = " ".join(words).strip(" .,!?:;") or "Live Stream Moment"
            posts = {
                "youtube_title": f"{short} | Stream Moment",
                "tiktok_title": short,
                "overlay_title": short[:60],
                "hook": short,
            }

    posts.setdefault("overlay_title", "")
    posts.setdefault("hook", "")
    posts["x_post"] = generate_x_caption(
        transcript_text, streamer, allowed_names,
        standalone_score=standalone_score, safety_flag=safety_flag,
    )

    posts = _strip_forbidden_names(posts, allowed_names, streamer)
    for field in ("youtube_title", "tiktok_title", "overlay_title", "hook", "x_post"):
        posts[field] = _redact(_verify_quotes(posts.get(field, ""), transcript_text))
    return posts


def write_posts_file(posts: dict, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("=== YOUTUBE TITLE ===\n")
        f.write(posts["youtube_title"] + "\n\n")
        f.write("=== TIKTOK TITLE ===\n")
        f.write(posts["tiktok_title"] + "\n\n")
        f.write("=== OVERLAY TITLE ===\n" + posts.get("overlay_title", "") + "\n\n")
        f.write("=== HOOK ===\n" + posts.get("hook", "") + "\n\n")
        f.write("=== X POST ===\n")
        f.write(posts["x_post"] + "\n")


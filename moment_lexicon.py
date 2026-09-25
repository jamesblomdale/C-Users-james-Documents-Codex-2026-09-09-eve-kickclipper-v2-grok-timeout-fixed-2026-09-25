"""
A Kick/IRL-specific vocabulary for deciding whether a transcript window
has real ACTION happening in it, versus being a name-list, mundane
chatter, or a bare mention with nothing occurring -- used by detect.py's
pre-filter (before any LLM call) and by the hard scoring caps (after).

This is a domain-specific keyword/phrase list, not creative writing --
the same kind of functional vocabulary list any content-classification
system uses (a spam filter's keyword list, a profanity filter, etc.).
"""

import re

# Physical / IRL action -- highest signal that something is actually
# happening on camera, not just being discussed.
PHYS = """
hit slap punch knock clock fight press crash smash shove push throw
run ran running over jumped jumping tackle choke beat knockout ko
pepperspray spray tase arrest cuff swat swatted pullup pulled pulledup
walkoff walked storm leave left kicked bounce
drive driving follow following stuck lost cooked totaled wreck
cut buzz shave hairline mog looksmax
""".split()

# Verbal combat / crashout -- arguments, callouts, confrontations.
TALK = """
crash crashout crashing yell scream screaming go goingoff
callout called expose exposed deny denied confess confesses admits
admit accuse accusing pressed arguing argument defensive
taunt taunting roast roasted glaze glazing flex flexing
apologize apology sorry
""".split()

# Reveal / plot twist -- something previously hidden comes out.
REVEAL = """
reveal reveals show showed showing admit admits confess
happen happened actually turnsout foundout realize realized
speechless shook shaken devastated embarrassed shocked surprised
caught catching leak leaked ghost ghosted
""".split()

# Reaction words -- often the actual title/overlay word.
REACT = """
speechless shook devastated embarrassed crying laughing screaming
panics panicked cooked dead silent quiet
""".split()

# Relationship / family drama -- a real Kick content lane.
DRAMA = """
cheat cheating cheated jump jumped setup setting
baby dna fridge shower hospital
knife herpes smoke cigarette
pray praying tantrum overstimulated
""".split()

# Money / status embarrassment.
STATUS = """
flex broke negative bank scam scammed sub goal
facecard cutline
""".split()

# Extra verbs called out explicitly as commonly-missed IRL/Kick slang.
EXTRA = """
lost stuck hit ran screaming pressing crashing slapped pulled ghosted
cooked following jumping fighting walking walked
""".split()

# Judgment-signal vocabulary: words that tend to show up around ego,
# money/status, hypocrisy, entitlement, and controversy -- the content
# categories detect.py's judgment scoring actually looks for. This is a
# CANDIDATE signal only (per detect.py's own prompt: a keyword does not
# mean a clip is good), used to cheaply widen what gets sent to the LLM
# beyond pure physical/verbal action words -- a bragging or hypocrisy
# moment often has no PHYS/TALK-bucket verb in it at all.
JUDGMENT = """
rich broke money cash bag millionaire billionaire mansion car cars
watch jewelry chain flex flexing brag bragging bragged
best goat greatest better untouchable unbeatable nobody king
arrogant ego cocky humble humility delusional delusion clueless
stupid idiot dumb ridiculous obviously wrong
hypocrite hypocrisy contradicts contradiction double standard
entitled deserve deserves rules apply respect disrespect disrespectful
cringe embarrassing awkward desperate trying
rejected rejection ghosted breakup cheat cheated betrayed betrayal
gamble gambling bet betting odds parlay casino slots
racist sexist bigot politics political immigration policing religion
""".split()

def _stem(word: str) -> str:
    """Lightweight stemming -- enough to match clocked->clock,
    screaming->scream, pulled->pull without a real NLP dependency."""
    if word.endswith("ing") and len(word) > 6:
        return word[:-3]
    if word.endswith("ed") and len(word) > 5:
        return word[:-2]
    return word


ACTION_STEMS = set()
for bucket in (PHYS, TALK, REVEAL, REACT, DRAMA, STATUS, EXTRA):
    for w in bucket:
        # Stem the lexicon entries the SAME way input text gets stemmed
        # at match time -- without this, a lexicon entry like "cooked"
        # never matches because input "cooked" gets stemmed down to
        # "cook" before comparison, and "cook" was never in the set.
        ACTION_STEMS.add(w)
        ACTION_STEMS.add(_stem(w))

# A handful of common single-syllable verbs double their final consonant
# before "-ed"/"-ing" in standard English spelling (slap -> slapped),
# which the naive suffix-stripping stemmer above can't undo correctly
# (slapped -> "slapp", not "slap"). Cheaper to list the base forms
# explicitly than to implement real consonant-doubling logic for a
# handful of words.
ACTION_STEMS |= {"slap", "clap", "grab", "stop", "drop", "rob", "shop", "hug", "beg"}

JUDGMENT_STEMS = set()
for w in JUDGMENT:
    JUDGMENT_STEMS.add(w)
    JUDGMENT_STEMS.add(_stem(w))

# Whole phrases that are almost always a real moment when combined with
# the streamer being in the scene -- matched as regex against the
# normalized (lowercased, punctuation-stripped) window text.
PHRASE_PATTERNS = [
    r"\b(i'?m cooked|we'?re cooked|i'?m dead)\b",
    r"\b(no keys|lost the key|car is stuck)\b",
    r"\b(clip it|that'?s a clip)\b",
    r"\b(walked off|walking off)\b",
    r"\b(showed up with a knife)\b",
    r"\b(i tried not to cheat)\b",
    r"\b(real life gta|we'?re actually in gta)\b",
    r"\b(on the (bonnet|hood))\b",
    r"\b(no eye contact)\b",
]

# Content words commonly used to open a genuinely quotable line -- used
# only as a soft signal, never a hard requirement.
QUOTABLE_MARKERS = [
    r"\bi (hate|tried|swear|knew|can'?t|would'?ve)\b",
    r"\byou (done|just|really|never)\b",
    r"\bthat'?s (for me to know|a clip|crazy)\b",
    r"\boh my god\b",
    r"\bwhat the (heck|hell)\b",
]

NOISE_WORDS = {
    "uh", "um", "like", "yeah", "yo", "bro", "chat", "okay", "ok",
    "alright", "you", "know", "i", "mean", "the", "a", "an", "and",
    "to", "of", "in", "on", "is", "it", "was", "were", "for", "with",
}


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9'\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def has_action(text: str) -> bool:
    """True if this window contains a real action verb/phrase from the
    Kick/IRL lexicon above -- the single most important signal for
    telling apart "something is happening" from "people are just
    talking/listing names"."""
    t = normalize(text)
    if any(re.search(p, t) for p in PHRASE_PATTERNS):
        return True
    stems = {_stem(w) for w in t.split()}
    return bool(stems & ACTION_STEMS)


def has_judgment_signal(text: str) -> bool:
    """
    True if this window contains vocabulary associated with ego, money/
    status, hypocrisy, entitlement, or controversy -- content that's
    often judgment-worthy WITHOUT containing a physical/verbal action
    verb (a brag or a hypocritical claim doesn't need a PHYS/TALK-bucket
    word to be real judgment material). A candidate signal only, same
    caveat as has_action: presence doesn't mean the window is good, and
    -- just as importantly -- absence doesn't mean it's bad on its own;
    this is combined with has_action and length in detect.py's cheap
    pre-filter, not used as a sole reject reason.
    """
    stems = {_stem(w) for w in normalize(text).split()}
    return bool(stems & JUDGMENT_STEMS)


def name_only_spam(text: str, priority_names: list[str], streamer: str) -> bool:
    """True if this window is essentially just several names being
    spoken with nothing else of substance and no real action."""
    t = normalize(text)
    content_words = [w for w in t.split() if w not in NOISE_WORDS]
    if len(content_words) >= 25:
        return False
    names = [n.strip().lower() for n in priority_names if n.strip().lower() != (streamer or "").lower()]
    hits = sum(1 for n in names if n in t)
    return hits >= 2 and not has_action(text)


def topic_overlap(topic_a: str, topic_b: str) -> float:
    """
    Fraction of shared CONTENT words between two topic labels (0.0-1.0),
    ignoring noise words. Used to decide whether a topic genuinely
    changed ("car stuck" -> "no keys" should NOT force a flush, since
    both are about the same stuck-car story) versus actually moving on
    to something unrelated. Comparing only the first word of each label
    is too fragile -- this compares the whole label's content words.
    """
    words_a = {w for w in normalize(topic_a).split() if w not in NOISE_WORDS}
    words_b = {w for w in normalize(topic_b).split() if w not in NOISE_WORDS}
    if not words_a or not words_b:
        return 0.0
    shared = words_a & words_b
    return len(shared) / min(len(words_a), len(words_b))

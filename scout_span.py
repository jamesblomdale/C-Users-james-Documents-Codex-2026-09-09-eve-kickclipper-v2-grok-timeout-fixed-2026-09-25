"""Safe scout candidate timestamp normalization."""
from __future__ import annotations

def _sec(value):
    if value is None or value == "": return None
    try: value=float(value)
    except (TypeError, ValueError): return None
    if value > 100_000: value/=1000.0
    return value

def accept_span(raw_start, raw_end, window_start, window_end, vod_end, min_len=8.0, max_len=90.0, pad=2.0):
    try:
        window_start=float(window_start); window_end=float(window_end); vod_end=float(vod_end or window_end)
        start=_sec(raw_start); end=_sec(raw_end)
        if start is None or end is None: return None,None,"missing_timestamp"
        if end < start: start,end=end,start
        window_len=max(.1,window_end-window_start)
        if end <= window_len+5 and start < window_start-5: start+=window_start; end+=window_start
        start=max(0.0,start); end=min(vod_end,end); start=max(window_start-pad,start); end=min(window_end+pad,end,vod_end)
        if end <= start: return None,None,"end_le_start"
        length=end-start; center=(start+end)/2.0
        if length < min_len:
            start=max(window_start,center-min_len/2); end=min(min(window_end,vod_end),start+min_len); start=max(0.0,end-min_len)
            return round(start,3),round(end,3),"expand_short"
        if length > max_len:
            start=max(window_start,center-max_len/2); end=min(min(window_end,vod_end),start+max_len)
            return round(start,3),round(end,3),"clamp_long"
        return round(start,3),round(end,3),"keep"
    except (TypeError,ValueError,OverflowError): return None,None,"invalid_timestamp"

def should_keep_local_window(text,min_words=8):
    words=[w for w in (text or '').split() if w]
    if len(words)<min_words:return False
    low=(text or '').lower()
    # This is a high-recall gate, not a virality score. Keep quiet reveals,
    # awkward turns and emotional changes alive for the LLM scout while still
    # dropping ordinary filler windows.
    signals=(
        'bro','nah','what','why','how','crazy','fight','lying','liar','lied',
        'stop','look','watch','wait','oh my','no way','oh shit','holy','shut',
        'dumb','pull up','hop out','security','cops','crash','headbutt',
        'speechless','silent','quiet','awkward','embarrass','embarrassing',
        'exposed','caught','admit','admission','confess','reveal','secret',
        'denied','rejected','kicked out','sorry','love','hate','scared','cry',
        'laugh','laughing','money','thousand','dollar','win','lost','fail',
        'again','actually','serious','promise','never','first time','clip it',
    )
    hits=sum(1 for x in signals if x in low)
    questions=low.count('?')
    exclamations=low.count('!')
    uppercase=sum(1 for w in (text or '').split() if len(w)>2 and w.isupper())
    # Preserve low-dialogue reactions and topic turns without allowing every
    # name-drop or profanity-only window through.
    return hits >= 1 or questions >= 2 or exclamations >= 2 or uppercase >= 2

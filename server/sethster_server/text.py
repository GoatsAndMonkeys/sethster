"""String helpers: slugs, accent-folding, surnames, track-title cleanup, segment inference."""
from __future__ import annotations

import re
import unicodedata

_TRANSLIT = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
                           "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "ð": "d", "ı": "i"})


def fold(s: str | None) -> str:
    """Lowercase, strip accents, collapse whitespace. Used for matching and search."""
    if not s:
        return ""
    s = s.translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip()


def norm_key(s: str | None) -> str:
    """Aggressive normalization for identity matching: folded, punctuation removed."""
    s = fold(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def slugify(s: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(s).replace("'", "")).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        if "-" in cut[max_len // 2:]:
            cut = cut[: cut.rfind("-")]
        slug = cut.strip("-")
    return slug or "unknown"


_PARTICLES = {"de", "del", "della", "di", "da", "von", "van", "der", "den", "le", "la", "du", "dos", "das", "ter", "ten"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_HONORIFICS = {"sir", "dame", "lord", "lady", "dr", "dr.", "maestro"}


def surname(name: str, particles: bool = True) -> str:
    """'Plácido Domingo' -> 'Domingo'; 'Giancarlo del Monaco' -> 'del Monaco'."""
    name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    parts = [p for p in name.replace(",", " ").split() if p]
    while parts and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    while len(parts) > 1 and parts[0].lower() in _HONORIFICS:
        parts.pop(0)
    if not parts:
        return name
    out = [parts[-1]]
    if particles:
        i = len(parts) - 2
        while i > 0 and parts[i].lower() in _PARTICLES:
            out.insert(0, parts[i])
            i -= 1
    return " ".join(out)


def sort_name(name: str, kind: str = "person") -> str:
    if kind != "person":
        return fold(re.sub(r"^(the|der|die|das|le|la|les|il|l')\s+", "", name, flags=re.I))
    sn = surname(name, particles=False)
    return fold(f"{sn} {name}")


ENSEMBLE_ROLE = re.compile(r"\b(orchestra|chorus|choir|ensemble|band|quartet|trio|quintet|consort)\b", re.I)
ENSEMBLE_NAME = re.compile(
    r"\b(orchestra|orchestre|orchester|orquesta|orchestr\w*|philharmoni\w*|symphony|sinfonia|sinfonietta|"
    r"chorus|choir|chor|coro|choeur|chœur|singers|ensemble|quartet|quartett|quatuor|trio|quintet|consort|"
    r"band|staatskapelle|kapelle|camerata|players|academy of|filarmonica|capella)\b",
    re.I,
)


def is_ensemble(name: str, role: str) -> bool:
    r = role.lower().strip()
    if r in {"orchestra", "chorus", "choir", "ensemble", "children's chorus", "band", "string quartet"}:
        return True
    if ENSEMBLE_ROLE.search(r) and not re.search(r"master|director|leader|soloist|member", r):
        return True
    return bool(ENSEMBLE_NAME.search(name)) and not re.search(r"master|director|conductor", r)


# ---- track titles -----------------------------------------------------------------------------

_NOTEWORTHY = re.compile(r"\d+:\d\d|\b\d+(\.\d+)?\s*(s|sec|min)\b|inferred|estimated|auditioned|approximate|tentative|"
                         r"nyphil|dirigent|lists|kbps", re.I)
_ABBREV_END = re.compile(r"(\b(op|no|nos|mvt|mvts|st|mr|mrs|dr|vs|ca|cf|incl|approx|vol|k|bwv|hwv|d|sc|wwv|wab|"
                         r"[A-Z])|\.\.)$", re.I)


_COPY_PREFIX = re.compile(r"^\d+\s*kbps[^\[:]{0,60}(\[[^\]]*\])?\s*:\s*", re.I)


def _drop_groups(s: str) -> str:
    # remove (...) and [...] groups that only carry timing / confidence notes
    def repl(m: re.Match) -> str:
        return "" if _NOTEWORTHY.search(m.group(0)) else m.group(0)

    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\s*\([^()]*\)", repl, s)
        s = re.sub(r"\s*\[[^\[\]]*\]", repl, s)
    return s


def clean_track_title(content: str, max_len: int = 120) -> str:
    """Turn a research track_map 'content' string into a short display title."""
    s = content.strip()
    # 'MP3 (320 kbps) duplicate ...' / '128 kbps 2-disc copy [..]: Act I' -> drop the copy prefix
    s = _COPY_PREFIX.sub("", s)
    s = _drop_groups(s)
    # first sentence only (split at '.'/';' outside brackets)
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch in ".;" and depth == 0 and re.match(r"\s+[A-Z'‘\"(]", s[i + 1:]):
            head = s[:i]
            if len(head) >= 6 and not _ABBREV_END.search(head):
                s = head
                break
    s = re.sub(r",\s*(\d+:\d\d|\d+\s*s)\s*$", "", s)  # trailing ', 2:30'
    s = re.sub(r"\s*[,;:]\s*(inferred|estimated|tentative)\.?$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .;,")
    if len(s) > max_len:
        cut = s[:max_len]
        s = cut[: cut.rfind(" ")] + "…" if " " in cut else cut + "…"
    return s or content[:max_len]


_SEGMENT_KEYWORDS: list[tuple[str, str]] = [
    (r"intermission|interval|entr'acte feature|met memory", "intermission"),
    (r"interview", "interview"),
    (r"applause|curtain calls?|ovation|booing", "applause"),
    (r"announcer|announcement|broadcast (opening|intro\w*|close|continuity)|continuity|presenter|"
     r"host (intro\w*|commentary|lead-in)|commentary|introduction by|sign-off|closing remarks|synopsis|"
     r"radio 3 (intro\w*|opening)|opening announcement", "announcer"),
    (r"quiz|unidentified|silence|tuning|tape noise", "other"),
]
_MUSIC_START = re.compile(r"^(act|acts|scene|mvt|movement|prologue|epilogue|prelude|overture|sinfonia|part|"
                          r"opera|symphony|concerto|complete|continuous segment|segment|disc|side|no\.|number|"
                          r"finale|aria|duet|trio|quartet|chorus|introduzione|first part|conclusion|end of act|"
                          r"continuation|i[-,: ]|ii[-,: ]|iii[-,: ]|iv[-,: ]|v[-,: ])", re.I)


def infer_segment(content: str) -> str:
    c = fold(content)
    c = _COPY_PREFIX.sub("", c)
    c = re.sub(r"^(probably|possibly|perhaps|likely|maybe)\s+", "", c)
    if _MUSIC_START.match(c):
        return "music"
    head = re.split(r"[(\[.;:]", c, maxsplit=1)[0]
    best: tuple[int, str] | None = None
    for pattern, seg in _SEGMENT_KEYWORDS:
        m = re.search(pattern, head)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), seg)
    return best[1] if best else "music"


def year_of(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"-?\d{3,4}", str(v))
    return int(m.group(0)) if m else None


def years_label(birth, death) -> str | None:
    b, d = year_of(birth), year_of(death)
    if b and d:
        return f"{b}–{d}"
    if b:
        return f"b. {b}"
    if d:
        return f"d. {d}"
    return None

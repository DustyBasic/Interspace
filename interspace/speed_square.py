"""Speed-square: a small utility of polymathic pattern-recognition
primitives. One tool, multiple utilities — any runner can call these
to score a hypothesis (is this pair a continuation? a near-duplicate?
a topic shift?) and combine the scores into a compound confidence.

Design intent:
  - All primitives are STATELESS and FAST. No I/O, no model loads,
    no allocation hot paths. Safe to call thousands of times per
    runner cycle.
  - Each primitive returns a score in [0, 1] OR a structured tuple
    when the relationship is non-scalar.
  - Primitives compose: runners combine them (weighted sum, max,
    threshold, etc.) for their own pass logic.
  - Self-improvement happens OUTSIDE this module — a future feedback
    layer tracks which scored emissions were accepted/rejected by the
    operator and adjusts the runner's weights. The primitives stay
    pure functions.

Polymathic = covers patterns drawn from multiple analytic traditions:
  - Information-theoretic (shingle overlap, sig collisions)
  - Linguistic (sentence-completion, anaphora, lexical chains)
  - Distributional (token Jaccard, char-class shift)
  - Structural (paragraph_index adjacency, source_file co-occurrence)

Each primitive's docstring includes:
  - What signal it detects
  - The 0..1 interpretation
  - What it does NOT capture (so callers know the limits)
"""

from __future__ import annotations

import re
from typing import Iterable

from .cross_refs import fnv1a_128_hex


# ---------------------------------------------------------------------------
# Sentence-completion + continuation signals
# ---------------------------------------------------------------------------

_SENTENCE_TERMINATOR_RE = re.compile(r'[.!?][\"\')\]\}]?\s*$')
_CONTINUER_END_RE = re.compile(r"[:;,→—-]\s*$")
_ANAPHORIC_START_RE = re.compile(
    r"^\s*(That|This|These|Those|It|Its|They|Their|Indeed|Therefore|"
    r"Furthermore|Moreover|However|But|And|So|Then|Hence|Thus|Also|"
    r"Additionally|Specifically|Namely)\b",
    re.IGNORECASE,
)
_LIST_MARKER_START_RE = re.compile(r"^\s*([-*•‣▪–—]|\d+[.)]|[a-zA-Z][.)])\s")


def ends_mid_thought_score(text: str) -> float:
    """How likely does this paragraph end mid-thought (incomplete)?

    Returns 0..1. Higher = more likely incomplete.
      1.0  — ends with continuation punctuation (`:` `;` `,` `—` `→`)
      0.7  — ends without ANY sentence terminator
      0.0  — ends with `.` `!` `?` (clean)

    Does NOT capture: rhetorical questions ending in `.`, lists with
    final period, code blocks. Score is a heuristic, not ground truth.
    """
    if not text:
        return 0.0
    tail = text.rstrip()
    if not tail:
        return 0.0
    if _CONTINUER_END_RE.search(tail):
        return 1.0
    if not _SENTENCE_TERMINATOR_RE.search(tail):
        return 0.7
    return 0.0


def starts_with_anaphora_score(text: str) -> float:
    """Does this paragraph open with a reference word that points
    backward (That, This, These, Indeed, Therefore, However, ...)?

    Returns 1.0 if it matches, 0.0 otherwise. Binary signal — refine
    by combining with ends_mid_thought_score of the previous paragraph.

    Does NOT capture: dropped subject continuations ("...and then we"),
    quoted continuations, pronoun chains beyond the first word.
    """
    if not text:
        return 0.0
    return 1.0 if _ANAPHORIC_START_RE.match(text.lstrip()) else 0.0


def starts_with_list_marker_score(text: str) -> float:
    """Does this paragraph start with a list bullet/number?
    `-` `*` `•` `1.` `a.` etc. → 1.0; otherwise 0.0.

    Pairs naturally with a previous paragraph ending in `:`.
    """
    if not text:
        return 0.0
    return 1.0 if _LIST_MARKER_START_RE.match(text) else 0.0


def continuation_likelihood(prev_text: str, next_text: str) -> float:
    """Compound score: how likely is the break between `prev` and
    `next` a SPURIOUS seam that artificially chopped a continuous
    thought? Range 0..1.

    Combines:
      - prev ends mid-thought (×0.6)
      - next opens with anaphora (×0.3)
      - prev ends `:` AND next is a list (×1.0 floor)

    A score ≥ 0.6 is a strong bind signal. The runner using this
    can choose its own threshold; default cutoff around 0.55 catches
    most real continuations while avoiding false positives on prose
    that just happens to end without a period.
    """
    if not prev_text or not next_text:
        return 0.0
    end_score = ends_mid_thought_score(prev_text)
    ana_score = starts_with_anaphora_score(next_text)
    list_score = (
        1.0
        if prev_text.rstrip().endswith(":") and starts_with_list_marker_score(next_text) > 0
        else 0.0
    )
    return max(list_score, 0.6 * end_score + 0.3 * ana_score)


# ---------------------------------------------------------------------------
# Lexical / distributional similarity
# ---------------------------------------------------------------------------

# Common stop words that don't help with content matching. Small list —
# intentionally conservative, since aggressive stop-word removal hurts
# precision on short paragraphs.
_STOP_WORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "being", "this", "that", "these", "those", "it", "its", "they", "their",
    "them", "we", "our", "us", "you", "your", "he", "she", "his", "her",
    "i", "me", "my", "but", "if", "then", "so", "not", "no", "do", "does",
    "did", "has", "have", "had", "will", "would", "can", "could", "may",
    "might", "must", "shall", "should", "about", "into", "over", "than",
    "such", "also", "just", "only", "any", "some", "all", "each", "every",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")


def content_tokens(text: str) -> set[str]:
    """Extract content tokens (lowercased) from text, excluding stop
    words and numerals. Returns a set, not a list — duplicates don't
    affect Jaccard similarity."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if len(tok) < 3 or tok in _STOP_WORDS:
            continue
        out.add(tok)
    return out


def lexical_chain_density(text_a: str, text_b: str) -> float:
    """Jaccard similarity over content tokens. 0..1.
      1.0 → identical content vocabulary
      ~0.4-0.6 → talking about the same thing
      ~0.1-0.3 → some overlap, probably topic-adjacent
      ~0.0 → unrelated

    Does NOT capture: semantic equivalents (synonyms), paraphrase,
    embedding-level similarity. A true semantic similarity layer
    would need TF-IDF or embeddings; this is the cheap baseline.
    """
    a = content_tokens(text_a)
    b = content_tokens(text_b)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return intersection / union


def lexical_chain_directional(text_prev: str, text_next: str) -> float:
    """Asymmetric chain score: fraction of `next`'s content tokens that
    also appear in `prev`. Useful for detecting "next continues the
    same topic that prev started" without penalizing prev for
    introducing tokens that next doesn't repeat.

      ~0.6+ → next is built mostly from prev's vocabulary (continuation)
      ~0.3-0.5 → drift but related
      ~0.0-0.2 → topic shift
    """
    a = content_tokens(text_prev)
    b = content_tokens(text_next)
    if not b:
        return 0.0
    overlap = len(a & b)
    return overlap / len(b)


# ---------------------------------------------------------------------------
# Information-theoretic / signature primitives
# ---------------------------------------------------------------------------

def sig128_prefix_collision(sig_a: str, sig_b: str, prefix_chars: int = 16) -> float:
    """Compare two sig128 hex strings by leading character count.
      1.0 — identical sigs (full content match)
      0.0 — first `prefix_chars` characters differ
      Else — proportional partial match.

    Note: FNV-1a is NOT locality-sensitive — partial sig matches are
    accidental, not semantic. This primitive is for fast exact-content
    bucketing and near-collision detection within bucket scans, not
    for similarity ranking.
    """
    if not sig_a or not sig_b:
        return 0.0
    if sig_a == sig_b:
        return 1.0
    # Match characters from the start
    matched = 0
    for ca, cb in zip(sig_a, sig_b):
        if ca == cb:
            matched += 1
        else:
            break
    return min(1.0, matched / prefix_chars) if prefix_chars > 0 else 0.0


def shingle_overlap(text_a: str, text_b: str, shingle_size: int = 80) -> float:
    """How much of the LAST `shingle_size` chars of A appears at the
    START of B? Detects "continuation across files" — paragraph A's
    tail and paragraph B's head are the same text.

      1.0 → B starts with A's exact tail (perfect continuation)
      ~0.5 → partial overlap (could be quotation, could be coincidence)
      0.0 → no overlap

    Uses a sliding match — checks A's last N chars against B's first
    N+50 chars and reports the longest prefix-suffix match.
    """
    if not text_a or not text_b:
        return 0.0
    tail = text_a[-shingle_size:].lstrip()
    head = text_b[:shingle_size + 50].rstrip()
    if not tail or not head:
        return 0.0
    # Try to find tail's longest suffix that is also a prefix of head
    best = 0
    upper = min(len(tail), len(head))
    for n in range(upper, 0, -1):
        if tail[-n:] == head[:n]:
            best = n
            break
    return min(1.0, best / shingle_size)


# ---------------------------------------------------------------------------
# Char-class distribution shift (cheap topic-shift detector)
# ---------------------------------------------------------------------------

def char_class_signature(text: str) -> dict[str, float]:
    """Distribution of character classes in `text` — used to detect
    qualitative shifts (prose ↔ code, English ↔ ALL-CAPS, etc.).

    Returns dict with keys: alpha_lower, alpha_upper, digit, punct,
    whitespace, symbol — values are fractions of total chars.
    """
    if not text:
        return {}
    counts = {"alpha_lower": 0, "alpha_upper": 0, "digit": 0,
              "punct": 0, "whitespace": 0, "symbol": 0}
    total = 0
    for c in text:
        total += 1
        if c.islower():
            counts["alpha_lower"] += 1
        elif c.isupper():
            counts["alpha_upper"] += 1
        elif c.isdigit():
            counts["digit"] += 1
        elif c.isspace():
            counts["whitespace"] += 1
        elif c in ".,!?;:\"'()[]{}-—":
            counts["punct"] += 1
        else:
            counts["symbol"] += 1
    if total == 0:
        return {}
    return {k: v / total for k, v in counts.items()}


def char_class_shift(text_a: str, text_b: str) -> float:
    """Total absolute difference between A's and B's char-class signatures.
    Range 0..2 (L1 distance on probability distributions).

      0.0 — same character composition (same content type)
      ~0.2 — minor stylistic drift
      ~0.5+ — strong shift (prose ↔ code, mixed-case ↔ ALL CAPS)
      ~1.0+ — entirely different content type

    Useful for detecting topic boundaries that survive lexical-chain
    analysis (e.g. switch from prose to code listing — both have content
    words but the char-class distribution differs sharply).
    """
    sig_a = char_class_signature(text_a)
    sig_b = char_class_signature(text_b)
    if not sig_a or not sig_b:
        return 0.0
    keys = set(sig_a) | set(sig_b)
    return sum(abs(sig_a.get(k, 0.0) - sig_b.get(k, 0.0)) for k in keys)


# ---------------------------------------------------------------------------
# Compound seam classifier (combines multiple primitives)
# ---------------------------------------------------------------------------

def spurious_seam_score(
    prev_text: str,
    next_text: str,
    prev_short: bool = False,
) -> float:
    """Aggregate confidence that the break between `prev` and `next`
    is spurious (should be bound). Combines:
      - continuation_likelihood    (linguistic continuation cues)
      - lexical_chain_directional   (topic continuity)
      - shingle_overlap             (literal continuation)
      - char_class_shift            (negative: shift discourages binding)

    The `prev_short` hint biases toward binding when prev is a short
    fragment (more likely to be a lead-in than a complete thought).

    Returns 0..1. Recommended thresholds:
      ≥ 0.7  — bind aggressively (low false-positive rate)
      ≥ 0.5  — bind cautiously
      < 0.5  — leave the seam alone
    """
    if not prev_text or not next_text:
        return 0.0
    cont = continuation_likelihood(prev_text, next_text)
    chain = lexical_chain_directional(prev_text, next_text)
    shingle = shingle_overlap(prev_text, next_text)
    shift = char_class_shift(prev_text, next_text)

    # Linear blend. Coefficients chosen so any single strong signal
    # crosses 0.5; multiple weak signals can also cross.
    # `continuation_likelihood` is the highest-precision feature, so
    # weighted most; shingle overlap is a strong but rare hit.
    score = 0.65 * cont + 0.20 * chain + 0.50 * shingle - 0.15 * min(1.0, shift)
    if prev_short:
        score += 0.10  # small bias toward absorbing short lead-ins
    return max(0.0, min(1.0, score))


# Convenience: list of all primitive names so future feedback layers
# can enumerate them when tracking accept/reject rates per pattern.
PRIMITIVES = (
    "ends_mid_thought_score",
    "starts_with_anaphora_score",
    "starts_with_list_marker_score",
    "continuation_likelihood",
    "content_tokens",
    "lexical_chain_density",
    "lexical_chain_directional",
    "sig128_prefix_collision",
    "shingle_overlap",
    "char_class_signature",
    "char_class_shift",
    "spurious_seam_score",
)

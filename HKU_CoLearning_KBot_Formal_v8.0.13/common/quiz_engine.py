from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Any

from knowledge_service.retrieval import fingerprint, jaccard, lexical_score, tokens

from .config import coze_quiz_types
from .coze_transport import AgentKind, CozeAgentRouter, CozeTransportError
from .quiz_parser import QuizValidationError, clean_text, normalize_quiz


QUIZ_TYPES = ("true_false", "mcq", "matching", "fill_blank", "short_answer")


class QuizUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class FocusFact:
    text: str
    fact_key: str
    chunk: dict[str, Any]


@dataclass(frozen=True)
class GeneratedQuiz:
    quiz: dict[str, Any]
    generation_source: str
    focus: FocusFact
    citations: list[dict[str, Any]]
    question_fingerprint: str
    coverage_exhausted: bool


_GENERIC_TERMS = {
    "use", "uses", "value", "values", "system", "service", "services", "information",
    "component", "components", "major", "class", "discussion", "discussions", "what",
    "example", "examples", "application", "applications", "introduction", "overview",
    "based", "originally", "unlike", "there", "this", "which", "frequency", "allocation",
    "is", "are", "was", "were", "be", "been", "do", "does", "did", "has", "have", "had",
    "can", "could", "will", "would", "should", "may", "might", "must",
}

# Fill Blank is an assessment of course vocabulary or a fact-bearing property,
# not an English grammar exercise.  These function words can occur verbatim in
# every source sentence, but blanking them does not test the course material.
_FILL_BLANK_FUNCTION_WORDS = {
    "a", "an", "the", "for", "to", "of", "in", "on", "at", "by", "from", "with",
    "without", "through", "between", "into", "onto", "over", "under", "about", "as",
    "and", "or", "but", "if", "then", "than", "that", "which", "who", "whose", "when",
    "where", "why", "how", "this", "these", "those", "it", "its", "they", "their",
    "there", "here", "only", "also", "very", "more", "most", "some", "any", "each",
    "all", "both", "either", "neither", "not", "no", "yes", "per", "via",
} | _GENERIC_TERMS
_FILL_NON_TECHNICAL = {
    "possible", "making", "make", "made", "available", "therefore", "however",
    "other", "different", "various", "according", "consider", "considered",
    "given", "used", "using", "obtained", "following", "shown", "example",
    "simple", "appropriate", "important", "mentioned", "several", "many",
}
_BAD_FRAGMENT_PATTERNS = (
    r"^class discussions?\b",
    r"^(?:what|how|why|when|where)\b",
    r"\b(?:introduction|overview|summary)\s*:?$",
    r"\be\.g\.?$",
)


def _used(history: dict | None, key: str) -> set[str]:
    return set(map(str, (history or {}).get(key, [])))


def _balanced_delimiters(value: str) -> bool:
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    return all(value.count(left) == value.count(right) for left, right in pairs)


def _balanced_quotes(value: str) -> bool:
    # PDF extraction commonly preserves curly quotation marks. Reject an
    # unmatched opening/closing mark so a fragment such as “area alarm is not
    # presented as a finished MCQ option. Apostrophes are intentionally ignored.
    return value.count("“") == value.count("”")


def _usable_fact(value: object) -> bool:
    text = clean_text(value).strip("•–- ")
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)
    lowered = text.casefold()
    if not (4 <= len(words) <= 40 and 18 <= len(text) <= 260):
        return False
    if text.endswith((":", "?", ":.")) or any(re.search(pattern, lowered) for pattern in _BAD_FRAGMENT_PATTERNS):
        return False
    if not _balanced_delimiters(text):
        return False
    if lowered.startswith(("copyright", "references", "further reading")):
        return False
    # Reject slide titles and noun-phrase labels that were accidentally stored
    # as facts. A quiz fact must express an action, state or relationship.
    has_predicate = bool(re.search(
        r"\b(?:is|are|was|were|be|been|has|have|had|consists?|includes?|contains?|"
        r"provides?|offers?|uses?|allows?|enables?|requires?|receives?|reaches?|"
        r"determines?|describes?|refers?|acts?|measures?|moves?|detects?|generates?|"
        r"obtains?|combines?|represents?|defines?|gives?|chooses?|classifies?|"
        r"minimi[sz]\w*|maximi[sz]\w*|"
        r"resists?|maintains?|causes?|produces?|emits?|stores?|connects?|switches?|"
        r"divides?|carries?|assigns?|depends?|changes?|computes?|located|designed|permitted|reflected|based)\b",
        lowered,
    ))
    if not has_predicate and not lowered.startswith(("to ", "use as ", "using ", "by ")):
        return False
    return True


def _usable_definition(value: object) -> bool:
    text = clean_text(value).strip("•–- ")
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)
    lowered = text.casefold()
    if not (4 <= len(words) <= 45 and 15 <= len(text) <= 300):
        return False
    if text.endswith((":", "?", ":.")) or any(re.search(pattern, lowered) for pattern in _BAD_FRAGMENT_PATTERNS):
        return False
    return _balanced_delimiters(text)


def _meaningful_term(value: object, allow_lowercase: bool = False) -> bool:
    term = clean_text(value).strip("•–-,:;. ")
    words = term.split()
    lowered = term.casefold()
    if not (1 <= len(words) <= 7 and 2 <= len(term) <= 70):
        return False
    if words[0].casefold() in {"a", "an", "the", "to", "use", "using", "class", "what", "how", "this", "that"}:
        return False
    if any(word.casefold() in {"that", "which", "who"} for word in words):
        return False
    if re.search(r"\b(?:is|are|was|were|consists?|includes?|contains?|provides?|offers?|uses?|allows?|enables?|requires?)\b", lowered):
        return False
    if lowered in _GENERIC_TERMS or any(re.search(pattern, lowered) for pattern in _BAD_FRAGMENT_PATTERNS):
        return False
    if term.endswith((".", ":", "?")) or not _balanced_delimiters(term):
        return False
    if not allow_lowercase and term[:1].islower() and not term.isupper():
        return False
    return True


def _section_is_dedicated(topic: str, section: object) -> bool:
    section_text = clean_text(section)
    if not topic or not section_text or any(marker in section_text for marker in ("•", "|", ";")):
        return False
    topic_terms = set(tokens(topic))
    section_terms = set(tokens(section_text))
    if not topic_terms or not topic_terms <= section_terms:
        return False
    # Parenthetical bullet/list titles such as "Location base technologies (• GPS)"
    # name several technologies and do not make every fact a GPS fact.
    if "(" in section_text and ")" in section_text and not section_text.casefold().startswith(topic.casefold()):
        return False
    return len(section_text.split()) <= 9


def _fact_matches_topic(topic: str, text: object, chunk: dict[str, Any]) -> bool:
    if not topic:
        return True
    if chunk.get("topic_scoped"):
        return True
    if lexical_score(topic, text) > 0:
        return True
    return _section_is_dedicated(topic, chunk.get("section"))




def _definition_subject(topic: str) -> str:
    raw = clean_text(topic).casefold()
    if not re.search(r"\b(?:definition|meaning|define)\b", raw):
        return ""
    words = tokens(topic)
    return " ".join(words[:5])


def _definition_relation_score(topic: str, text: object) -> float:
    subject = _definition_subject(topic)
    value = clean_text(text)
    if not subject or not value:
        return 0.0
    subject_pattern = r"\b" + r"\s+".join(re.escape(word) + r"s?" for word in subject.split()) + r"\b"
    if re.search(r"\bby\s+definition\b", value, re.IGNORECASE) and re.search(subject_pattern, value, re.IGNORECASE):
        return 1.20
    direct_pattern = (
        r"(?:^|\b(?:the|a|an|one|1)\s+)"
        + subject_pattern
        + r"\s+(?:is|are|means|refers\s+to|is\s+defined\s+as)\b"
    )
    if re.search(direct_pattern, value, re.IGNORECASE):
        return 0.70
    return 0.0

def select_focus(chunks: list[dict[str, Any]], history: dict | None = None, topic: str = "") -> FocusFact:
    used_facts = _used(history, "facts")
    if _definition_subject(topic):
        definition_candidates: list[tuple[float, FocusFact]] = []
        for chunk in chunks:
            for fact in list(chunk.get("facts") or []):
                text = clean_text(fact.get("text"))
                fact_key = str(fact.get("fact_key") or f"F-{fingerprint(text)}")
                bonus = _definition_relation_score(topic, text)
                if bonus <= 0 or not (_usable_fact(text) or _usable_definition(text)):
                    continue
                score = lexical_score(topic, f"{chunk.get('section', '')} {text}") + bonus
                definition_candidates.append((score - (0.18 if fact_key in used_facts else 0.0), FocusFact(text, fact_key, chunk)))
        if definition_candidates:
            definition_candidates.sort(key=lambda item: item[0], reverse=True)
            return definition_candidates[0][1]
    for chunk in chunks:
        facts = [fact for fact in list(chunk.get("facts", [])) if _usable_fact(fact.get("text"))]
        facts.sort(
            key=lambda fact: lexical_score(topic, f"{chunk.get('section', '')} {fact.get('text', '')}") if topic else 0.5,
            reverse=True,
        )
        for fact in facts:
            fact_key = str(fact.get("fact_key") or f"F-{fingerprint(fact.get('text'))}")
            text = clean_text(fact.get("text"))
            if fact_key not in used_facts and _fact_matches_topic(topic, text, chunk):
                return FocusFact(text, fact_key, chunk)
    for chunk in chunks:
        text = clean_text(chunk.get("text"))
        if text:
            sentence = next(
                (part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if _usable_fact(part)),
                "",
            )
            if sentence:
                key = f"F-{fingerprint(sentence)}"
                if key not in used_facts and _fact_matches_topic(topic, sentence, chunk):
                    return FocusFact(sentence, key, chunk)
    reusable: list[tuple[float, str, str, dict[str, Any]]] = []
    for chunk in chunks:
        for fact in chunk.get("facts", []):
            text = clean_text(fact.get("text"))
            score = lexical_score(topic, f"{chunk.get('section', '')} {text}") if topic else 0.5
            if _usable_fact(text) and _fact_matches_topic(topic, text, chunk):
                reusable.append((score, text, str(fact.get("fact_key") or f"F-{fingerprint(text)}"), chunk))
    if reusable:
        reusable.sort(key=lambda item: item[0], reverse=True)
        _, text, fact_key, chunk = reusable[0]
        return FocusFact(text, fact_key, chunk)
    raise QuizUnavailableError("No relevant course-note fact is available for this topic.")


_TRUE_FALSE_AUXILIARY_RE = re.compile(
    r"\b(?:is|are|was|were|can|could|will|would|should|has|have|had|does|do|did)\b",
    re.IGNORECASE,
)
_TRUE_FALSE_FRAGMENT_STARTS = (
    "based on ", "according to ", "using ", "use as ", "by ", "to ", "originally ", "unlike ",
)


def _readable_lecture_text(value: object) -> bool:
    """Reject damaged PDF extraction, not legitimate Unicode Greek symbols.

    SymbolMT private-use glyphs must first be decoded by the PDF parser.  A
    validation failure is preferable to claiming that a scrambled equation is
    a correct answer.  Never infer the missing formula from nearby tokens.
    """
    text = clean_text(value)
    if not text or any(0xE000 <= ord(char) <= 0xF8FF for char in text):
        return False
    if "\ufffd" in text or re.search(r"\b(?:fig|eq|figure|equation)\.?\s*$", text, re.IGNORECASE):
        return False
    if re.search(r"(?:[.!?]\s*){2,}$", text):
        return False
    if re.search(r"\b(?:i\.?e\.?|e\.?g\.?)\s*$", text, re.IGNORECASE):
        return False
    if re.search(r"\bis\s+(?:actually\s+)?related\s+to\b", text, re.IGNORECASE):
        return False
    # Broken PDF equation layout can erase subscripts and yield a choice rule
    # that compares apparently identical expressions. Never quiz on it as if
    # it were a complete, mathematically reliable proposition.
    if re.search(r"\bdecide\b.*\bif\b.*\b(?:else|otherwise)\b", text, re.IGNORECASE) and re.search(r"[ωαλΣ]", text):
        return False
    # An orphaned subscript tail ('j j .') or repeated posterior expression
    # with lost indices is not a recoverable mathematical statement.
    if re.search(r"\b[ij]\s+[ij]\s*[.]?$", text, re.IGNORECASE) and "(" in text:
        return False
    if len(re.findall(r"P\s*\(\s*ω\s*\|\s*x\s*\)", text)) >= 2 and re.search(r"[<>≤≥]", text):
        return False
    # A word boundary erased between the relative word and a math verb.
    if re.search(r"\b(?:that|which|and|because)(?:maximi[sz]\w*|minimi[sz]\w*|gives?|describes?|combines?)\b", text, re.IGNORECASE):
        return False
    # Equation text with repeated orphaned indices almost never makes a
    # valid self-contained sentence, even when the brackets are balanced.
    if len(re.findall(r"(?<!\w)[0-9](?!\w)", text)) >= 8 and len(re.findall(r"[A-Za-z]{3,}", text)) < 12:
        return False
    return True


def _tf_topic_match(topic: str, value: str, chunk: dict[str, Any]) -> bool:
    """Require evidence for the *whole* short topic, not one common word.

    A retrieved document header often contains 'Bayes' and 'classifier'.
    Sharing one such term is insufficient to convert an unrelated fact into
    a True/False question on Bayes risk or normal-density classification.
    """
    if not _fact_matches_topic(topic, value, chunk):
        return False
    query_terms = set(tokens(topic))
    if len(query_terms) <= 1 or chunk.get("topic_scoped"):
        # A validated course-outline page range is itself a legitimate scope
        # for a broad topic such as 'mobile communication'.
        return True
    fact_terms = set(tokens(value))
    matched = len(query_terms & fact_terms)
    required = 2 if len(query_terms) <= 3 else max(2, (len(query_terms) + 1) // 2)
    if matched >= required:
        return True
    return _section_is_dedicated(topic, chunk.get("section"))


def _usable_true_false_fact(value: object) -> bool:
    """Require a complete proposition rather than a slide fragment.

    A slightly longer definitional sentence is acceptable because lecture
    slides often keep a full definition in one bullet.
    """
    if not (_usable_fact(value) or _usable_definition(value)) or not _readable_lecture_text(value):
        return False
    text = _polish_sentence(value)
    lowered = text.casefold()
    if lowered.startswith(_TRUE_FALSE_FRAGMENT_STARTS) and not lowered.startswith("by definition"):
        return False
    if len(re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)) < 4:
        return False
    if not _balanced_quotes(text):
        return False
    # A finite verb/auxiliary gives the statement a clear truth condition.
    return bool(
        _TRUE_FALSE_AUXILIARY_RE.search(text)
        or re.search(
            r"\b(?:consists?|includes?|contains?|provides?|offers?|uses?|allows?|enables?|"
            r"requires?|receives?|reaches?|determines?|describes?|refers?|transmits?|"
            r"operates?|follows?|supports?|manages?|converts?|calculates?|acts?|measures?|"
            r"moves?|detects?|generates?|resists?|maintains?|causes?|produces?|emits?|"
            r"stores?|connects?|switches?|divides?|carries?|assigns?|depends?|changes?|computes?|"
            r"obtains?|combines?|represents?|defines?|gives?|chooses?|classifies?|"
            r"minimi[sz]\w*|maximi[sz]\w*)\b",
            text,
            re.IGNORECASE,
        )
    )


def _true_false_focus(
    chunks: list[dict[str, Any]],
    history: dict | None,
    topic: str,
    correct_index: int,
) -> FocusFact:
    """Select a complete fact that can support the requested truth value."""
    used_facts = _used(history, "facts")
    candidates: list[tuple[int, int, float, int, FocusFact]] = []
    seen: set[str] = set()

    def add(text: object, fact_key: object, chunk: dict[str, Any], order: int, stored_fact: bool) -> None:
        value = clean_text(text)
        key = str(fact_key or f"F-{fingerprint(value)}")
        if key in seen or not _usable_true_false_fact(value) or not _tf_topic_match(topic, value, chunk):
            return
        seen.add(key)
        body = f"{chunk.get('section', '')} {value}"
        score = lexical_score(topic, body) if topic else 0.5
        score += _definition_relation_score(topic, value)
        topic_terms = set(tokens(topic))
        body_terms = set(tokens(body))
        if topic_terms and topic_terms <= body_terms:
            score += 0.30
        if clean_text(value).casefold().startswith(clean_text(topic).casefold() + " "):
            score += 0.18
        candidate = FocusFact(value, key, chunk)
        if correct_index == 1 and _false_variant(value, key) is None:
            return
        candidates.append((0 if key in used_facts else 1, 1 if stored_fact else 0, score, -order, candidate))

    order = 0
    for chunk in chunks:
        for fact in list(chunk.get("facts") or []):
            add(fact.get("text"), fact.get("fact_key"), chunk, order, True)
            order += 1
        normalized = re.sub(r"\n[•–-]\s*", ". ", str(chunk.get("text") or ""))
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", normalized):
            add(sentence, "", chunk, order, False)
            order += 1

    if not candidates:
        raise QuizUnavailableError("No complete course-note statement is available for a True/False question on this topic.")
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    # Empty topic = chapter-wide random: sample among the strongest unused facts.
    if not str(topic or "").strip():
        unused = [item for item in candidates if item[0] == 1]
        pool = unused or candidates
        top = pool[: min(8, len(pool))]
        return random.choice(top)[4]
    return candidates[0][4]


def citation(chunk: dict[str, Any], excerpt: str | None = None) -> dict[str, Any]:
    return {
        "reference_id": 0,
        "document_id": chunk.get("document_id", ""), "source_file": chunk.get("source_file", ""),
        "chapter_id": chunk.get("chapter_id", ""), "chapter": chunk.get("chapter", ""),
        "section_id": chunk.get("section_id", ""), "section": chunk.get("section", ""),
        "page": chunk.get("page"), "excerpt": clean_text(excerpt or chunk.get("text"))[:700],
        "chunk_id": chunk.get("chunk_id", ""), "knowledge_key": chunk.get("knowledge_key", ""),
    }


def _all_context(chunks: list[dict[str, Any]]) -> str:
    return "\n".join(str(chunk.get("text") or "") for chunk in chunks)


def _phrase_in_notes(value: object, context: str) -> bool:
    phrase = clean_text(value).casefold()
    if not phrase:
        return False
    normalized_context = clean_text(context).casefold()
    if phrase in normalized_context:
        return True
    phrase_tokens = set(tokens(phrase))
    return bool(phrase_tokens) and len(phrase_tokens & set(tokens(context))) / len(phrase_tokens) >= 0.85


def _usable_short_answer_point(value: object, section: object = "") -> bool:
    text = clean_text(value).strip("•–- ")
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)
    lowered = text.casefold()
    section_text = clean_text(section).strip(" .:;-").casefold()
    if not (4 <= len(words) <= 55 and 18 <= len(text) <= 360):
        return False
    if text.endswith((":", "?")) or lowered.startswith(("class discussions", "references", "further reading")):
        return False
    if section_text and lowered.rstrip(".") == section_text.rstrip("."):
        return False
    if re.match(r"^(?:what|how|why|when|where)\b", lowered):
        return False
    return _balanced_delimiters(text) and _balanced_quotes(text)


def _atomic_definition_points(text: object) -> list[str]:
    """Split two genuinely independent source claims about one subject.

    E.g. "A point source is infinitely small and emits light evenly ...".
    Compound objects ("holds and protects the lamp") are not split, nor are
    equations, lists, or statements lacking a clear shared subject.
    """
    value = clean_text(text).strip(" .;:")
    matched = re.fullmatch(
        r"((?:a|an|the)\s+[A-Za-z][A-Za-z -]{1,55}?)\s+"
        r"(is|are)\s+([^;:,.]{4,90}?)\s+and\s+"
        r"(emits?|produces?|reflects?|transmits?|detects?|measures?)\s+"
        r"([^;:,.]{6,130})",
        value, re.IGNORECASE,
    )
    if not matched:
        return [value + "."] if value else []
    subject, linking_verb, first, action, second = matched.groups()
    if len(first.split()) < 2 or len(second.split()) < 2:
        return [value + "."]
    return [
        f"{subject} {linking_verb} {first}.",
        f"{subject} {action} {second}.",
    ]


def _short_answer_evidence(
    focus: FocusFact,
    chunks: list[dict[str, Any]],
    topic: str = "",
    max_points: int = 4,
) -> list[tuple[str, dict[str, Any]]]:
    """Select a coherent Short Answer rubric without creating an open list.

    Small, explicit bullet groups (two to four relations) are treated as one
    multi-point rubric.  A slide with many possible examples is not truncated to
    an arbitrary first four; in that case the question is narrowed to the selected
    focus fact so another valid set of examples is not unfairly marked wrong.
    """
    source_file = clean_text(focus.chunk.get("source_file"))
    page = focus.chunk.get("page")
    if _definition_subject(topic):
        # A narrow definition topic is not a request for all glossary terms
        # on the slide. In a semicolon-separated definition, keep only the
        # clause which actually defines the requested subject.
        point = clean_text(focus.text)
        subject = _definition_subject(topic)
        for clause in re.split(r";\s+", point):
            if subject and re.search(rf"\b{re.escape(subject)}\b", clause, re.IGNORECASE):
                point = clause
                break
        return [(statement, focus.chunk) for statement in _atomic_definition_points(point)]
    section = clean_text(focus.chunk.get("section"))
    scoped: list[tuple[int, dict[str, Any]]] = []
    for chunk in chunks:
        same_chunk = clean_text(chunk.get("chunk_id")) == clean_text(focus.chunk.get("chunk_id"))
        same_page = (
            source_file and clean_text(chunk.get("source_file")) == source_file
            and page is not None and chunk.get("page") == page
        )
        same_section = section and clean_text(chunk.get("section")) == section
        if same_chunk or same_page or same_section:
            scoped.append((80 if same_chunk else 65 if same_page else 40, chunk))
    if not scoped:
        scoped = [(80, focus.chunk)]

    def distinct(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        output: list[tuple[str, dict[str, Any]]] = []
        for text, chunk in items:
            value = _polish_sentence(text)
            if value.count("“") != value.count("”"):
                value = value.replace("“", "").replace("”", "")
            if not _usable_short_answer_point(value, chunk.get("section")):
                continue
            if any(
                value.casefold() in existing.casefold()
                or existing.casefold() in value.casefold()
                or jaccard(value, existing) >= 0.78
                for existing, _ in output
            ):
                continue
            output.append((value, chunk))
        return output

    # Explicit label/detail relations are the strongest evidence that a slide is
    # asking for a finite list (for example Pull, Push and Tracking).
    relation_items: list[tuple[str, dict[str, Any]]] = []
    for _priority, chunk in sorted(scoped, key=lambda item: item[0], reverse=True):
        for relation in _bullet_relations(chunk.get("text")):
            label = clean_text(relation.get("label")).rstrip(" .:;-")
            definition = clean_text(relation.get("definition"))
            if label and definition:
                relation_items.append((f"{label}: {definition}", chunk))
    relation_points = distinct(relation_items)
    if 2 <= len(relation_points) <= max_points:
        focus_value = _polish_sentence(focus.text)
        if not any(
            focus_value.casefold() in point.casefold()
            or point.casefold() in focus_value.casefold()
            or jaccard(focus_value, point) >= 0.68
            for point, _ in relation_points
        ):
            relation_points.insert(0, (focus_value, focus.chunk))
        if 2 <= len(relation_points) <= max_points:
            return relation_points

    general_items: list[tuple[int, float, str, dict[str, Any]]] = []

    def add(text: object, chunk: dict[str, Any], priority: int) -> None:
        value = _polish_sentence(text)
        if not _usable_short_answer_point(value, chunk.get("section")):
            return
        relevance = lexical_score(topic, f"{chunk.get('section', '')} {value}") if topic else 0.5
        general_items.append((priority, relevance, value, chunk))

    add(focus.text, focus.chunk, 100)
    for priority, chunk in scoped:
        for fact in list(chunk.get("facts") or []):
            add(fact.get("text"), chunk, priority)
        normalized = re.sub(r"\n[•–-]\s*", ". ", str(chunk.get("text") or ""))
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", normalized):
            add(sentence, chunk, priority - 2)

    general_items.sort(key=lambda item: (item[0], item[1]), reverse=True)
    general_points = distinct([(text, chunk) for _priority, _relevance, text, chunk in general_items])
    if 1 <= len(general_points) <= max_points:
        if len(general_points) == 1:
            atomic = _atomic_definition_points(general_points[0][0])
            if 2 <= len(atomic) <= max_points:
                return [(point, general_points[0][1]) for point in atomic]
        return general_points
    atomic = _atomic_definition_points(focus.text)
    if 2 <= len(atomic) <= max_points:
        return [(point, focus.chunk) for point in atomic]
    return [(_polish_sentence(focus.text), focus.chunk)]


def _short_answer_subject(focus: FocusFact, topic: str = "") -> str:
    section = clean_text(focus.chunk.get("section")).strip(" .:;-")
    if (
        section and len(section.split()) <= 12
        and not any(mark in section for mark in ("•", "|", ";"))
        and not re.match(r"^(?:what|how|why|when|where)\b", section, re.IGNORECASE)
        and not section.endswith("?")
    ):
        return section
    return clean_text(topic) or "the selected course-note concept"


def _short_answer_relation_labels(points: list[str]) -> list[str]:
    labels: list[str] = []
    for point in points:
        if ":" not in point:
            continue
        label, definition = point.split(":", 1)
        label = clean_text(label).strip(" .:;-")
        definition = clean_text(definition)
        if label and definition and 1 <= len(label.split()) <= 10:
            labels.append(label)
    return labels


def _joined_labels(labels: list[str]) -> str:
    if len(labels) <= 1:
        return labels[0] if labels else ""
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _short_answer_question(
    subject: str,
    points: list[str],
    focus: FocusFact | None = None,
    chunks: list[dict[str, Any]] | None = None,
    topic: str = "",
) -> str:
    point_count = len(points)
    if _definition_subject(topic) and point_count == 1:
        return f"According to the course notes, what is the definition of {_definition_subject(topic)}?"
    labels = _short_answer_relation_labels(points)
    context_chunks = list(chunks or [])
    if focus is not None:
        same_section = [
            chunk for chunk in context_chunks
            if clean_text(chunk.get("section")) == clean_text(focus.chunk.get("section"))
        ]
        context = "\n".join(clean_text(chunk.get("text")) for chunk in (same_section or [focus.chunk]))
    else:
        context = "\n".join(clean_text(chunk.get("text")) for chunk in context_chunks)

    if point_count > 1 and len(labels) == point_count:
        application_labels = [label for label in labels if re.search(r"\bapplications?\b", label, re.IGNORECASE)]
        principle_labels = [label for label in labels if label not in application_labels]
        if application_labels and principle_labels:
            owner = clean_text(topic) or subject or "the selected concept"
            return (
                f"According to the course notes, explain {_joined_labels(principle_labels)} "
                f"and state the listed application examples of {owner}?"
            )
        structure_match = re.search(
            r"\bstructure of (?:a|an|the)\s+([^:\n.]{2,80})",
            context,
            re.IGNORECASE,
        )
        if structure_match:
            owner = clean_text(structure_match.group(1)).strip(" .:;-")
            return f"According to the course notes, name and explain the {point_count} components of the {owner} structure?"

        values_match = re.match(r"^Values of\s+(.+)$", subject, re.IGNORECASE)
        if values_match:
            owner = clean_text(values_match.group(1)).strip(" .:;-")
            return f"According to the course notes, state and explain the {point_count} values of {owner}?"

        if point_count <= 4:
            owner = subject or clean_text(topic)
            if "structure" in owner.casefold():
                return f"According to the course notes, name and explain the {point_count} components listed under {owner}?"
            preposition = "in" if re.fullmatch(r"[A-Z]{2,8}", owner) else "under"
            return f"According to the course notes, explain {_joined_labels(labels)} {preposition} {owner}?"

    if labels and point_count == len(labels) + 1:
        first = clean_text(points[0])
        concept_match = re.match(
            r"^(?:A|An|The)?\s*(.{2,80}?)\s+(?:is|are|means|refers to|describes)\b",
            first,
            re.IGNORECASE,
        )
        concept = clean_text(concept_match.group(1)).strip(" .:;-") if concept_match else subject
        return (
            f"According to the course notes, define {concept} and explain "
            f"{_joined_labels(labels)}?"
        )

    if point_count > 1:
        if focus and point_count == 2 and all(
            re.match(r"^(?:A|An|The)\s+(.{2,65}?)\s+(?:is|are|emits?|produces?|reflects?|transmits?)\b", item, re.IGNORECASE)
            for item in points
        ):
            subjects = [re.match(r"^(?:A|An|The)\s+(.{2,65}?)\s+(?:is|are|emits?|produces?|reflects?|transmits?)\b", item, re.IGNORECASE).group(1) for item in points]
            if subjects[0].casefold() == subjects[1].casefold():
                return f"According to the course notes, what are the two defining characteristics of {subjects[0]}?"
        return f"According to the course notes, state and explain the {point_count} points about {subject}?"
    point = clean_text(points[0]) if points else ""
    relation = re.match(
        r"^(.{2,90}?)\s+(?:is|are|was|were|means|refers to|describes|provides|uses|allows|enables|requires)\b",
        point,
        re.IGNORECASE,
    )
    if relation:
        concept = clean_text(relation.group(1)).strip(" .:;-")
        return f"According to the course notes, what is stated about {concept}?"
    if ":" in point:
        label = clean_text(point.split(":", 1)[0]).strip(" .:;-")
        if 1 <= len(label.split()) <= 10:
            return f"According to the course notes, explain the item “{label}” under {subject}?"
    lead = clean_text(point.split(",", 1)[0]).strip(" .:;-")
    if 1 <= len(lead.split()) <= 8 and lead.casefold() != subject.casefold():
        return f"What detail do the course notes give for “{lead}” under {subject}?"
    return f"According to the course notes, explain the selected point about {subject}?"


def _short_answer_question_matches_evidence(
    question: object,
    focus: FocusFact,
    points: list[str],
    topic: str = "",
) -> bool:
    question_text = clean_text(question)
    if not question_text.endswith("?"):
        return False
    if _definition_subject(topic) and not set(tokens(_definition_subject(topic))) <= set(tokens(question_text)):
        return False
    question_terms = {term for term in tokens(question_text) if term not in _GENERIC_TERMS}
    section_terms = {term for term in tokens(focus.chunk.get("section")) if term not in _GENERIC_TERMS}
    label_terms = {
        term
        for label in _short_answer_relation_labels(points)
        for term in tokens(label)
        if term not in _GENERIC_TERMS
    }
    requested_subject = set(tokens(_definition_subject(topic))) if topic else set()
    # The direct validator does not receive the topic. An explicitly worded
    # definition question can still be checked against its focus fact; for
    # general prompts, preserve the stricter section/label matching rule.
    focus_subject_terms = (
        {word for word in tokens(focus.text) if word not in _GENERIC_TERMS}
        if re.search(r"\bdefinition of\b", question_text, re.I) else set()
    )
    if not question_terms or not (question_terms & (section_terms | label_terms | requested_subject | focus_subject_terms)):
        return False
    labels = _short_answer_relation_labels(points)
    has_application_point = any(re.search(r"\bapplications?\b", label, re.IGNORECASE) for label in labels)
    if has_application_point and re.search(r"\bprinciples?\b", question_text, re.IGNORECASE):
        # A mixed rubric containing an application bullet must not be described
        # as four principles. The backend-generated question names the different
        # expectations explicitly.
        return False
    if 0 < len(labels) < len(points):
        # One leading definition followed by labelled relations is a legitimate
        # structure (for example: define LBS, then explain Pull/Push/Tracking).
        first = clean_text(points[0]) if points else ""
        leading_definition = bool(re.match(
            r"^(?:A|An|The)?\s*.{2,80}?\s+(?:is|are|means|refers to|describes)\b",
            first, re.IGNORECASE,
        ))
        if not (len(labels) == len(points) - 1 and leading_definition):
            # Other mixed labelled/unlabelled evidence is too easy for a fluent
            # Agent to describe inaccurately. Use the backend question instead.
            return False
    if len(points) > 1:
        number_words = {2: "two", 3: "three", 4: "four"}
        count_present = bool(re.search(rf"\b{len(points)}\b", question_text)) or number_words.get(len(points), "") in question_text.casefold()
        labels = _short_answer_relation_labels(points)
        labels_present = bool(labels) and all(clean_text(label).casefold() in question_text.casefold() for label in labels)
        clearly_plural_traits = bool(re.search(r"\b(?:characteristics|properties|features|requirements|conditions)\b", question_text, re.IGNORECASE))
        if not count_present and not labels_present and not (len(points) == 2 and clearly_plural_traits):
            return False
    return True


def _complete_point_sentence(point: object) -> str:
    text = clean_text(point).rstrip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _short_answer_reference(points: list[str]) -> str:
    if len(points) == 1:
        return _complete_point_sentence(points[0])
    return "A complete answer should include: " + " ".join(
        f"({index}) {_complete_point_sentence(point)}" for index, point in enumerate(points, 1)
    )


def _short_answer_explanation(points: list[str]) -> str:
    if len(points) == 1:
        return f"The course notes state: {_complete_point_sentence(points[0])}"
    return "The course notes give the following required points: " + " ".join(
        f"({index}) {_complete_point_sentence(point)}" for index, point in enumerate(points, 1)
    )


def _short_answer_citations(evidence: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: dict[tuple[str, object, str], int] = {}
    for text, chunk in evidence:
        key = (clean_text(chunk.get("source_file")), chunk.get("page"), clean_text(chunk.get("chunk_id")))
        if key in seen:
            previous = output[seen[key]]
            if text.casefold() not in previous["excerpt"].casefold():
                previous["excerpt"] = clean_text(previous["excerpt"] + " " + text)[:700]
            continue
        item = citation(chunk, text)
        item["reference_id"] = len(output) + 1
        seen[key] = len(output)
        output.append(item)
    return output


def _prepare_short_answer_quiz(
    quiz: dict[str, Any],
    focus: FocusFact,
    chunks: list[dict[str, Any]],
    topic: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evidence = _short_answer_evidence(focus, chunks, topic)
    points = [text for text, _chunk in evidence]
    subject = _short_answer_subject(focus, topic)
    authoritative_question = _short_answer_question(subject, points, focus, chunks, topic)
    candidate_question = clean_text(quiz.get("question"))
    question = (
        candidate_question
        if _short_answer_question_matches_evidence(candidate_question, focus, points, topic)
        else authoritative_question
    )
    completed = {
        "type": "short_answer",
        "question": question,
        "key_points": points,
        "reference": _short_answer_reference(points),
        "explanation": _short_answer_explanation(points),
    }
    return completed, _short_answer_citations(evidence)


def _covers_short_answer_point(container: object, point: object) -> bool:
    body = clean_text(container)
    required = clean_text(point)
    if not body or not required:
        return False
    return (
        required.casefold() in body.casefold()
        or jaccard(body, required) >= 0.62
        or len(set(tokens(required)) & set(tokens(body))) / max(1, len(set(tokens(required)))) >= 0.78
    )


def _mcq_option_quality(option: object) -> bool:
    text = clean_text(option).strip("•–- ")
    if not text or len(text) > 320 or text.endswith((":", "?")) or not _readable_lecture_text(text):
        return False
    if any(re.search(pattern, text.casefold()) for pattern in _BAD_FRAGMENT_PATTERNS):
        return False
    if not _balanced_delimiters(text) or not _balanced_quotes(text):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)
    if len(words) == 1 and words[0].casefold() in _GENERIC_TERMS:
        return False
    return 1 <= len(words) <= 55


def validate_grounding(
    quiz: dict[str, Any],
    chunks: list[dict[str, Any]],
    term_candidates: list[dict[str, Any]],
    focus: FocusFact | None = None,
) -> None:
    context = _all_context(chunks)
    quiz_type = quiz["type"]
    if quiz_type == "true_false":
        question = clean_text(quiz.get("question"))
        explanation = clean_text(quiz.get("explanation"))
        correct_index = int(quiz.get("correct_index", -1))
        if correct_index not in {0, 1}:
            raise QuizValidationError("True/False needs a valid True or False answer key.")
        if not explanation or not _phrase_in_notes(explanation, context):
            raise QuizValidationError("The True/False explanation is not grounded in the supplied notes.")
        if correct_index == 0:
            if not _phrase_in_notes(question, context):
                raise QuizValidationError("The true statement is not sufficiently grounded in the selected notes.")
        else:
            # A false statement is intentionally absent from the notes, so it
            # cannot be validated with an exact phrase lookup.  Require it to
            # remain a close, one-detail contradiction of the grounded
            # explanation instead of accepting an arbitrary unsupported claim.
            if not _bounded_false_contradiction(question, explanation):
                raise QuizValidationError("The false statement is not a bounded contradiction of the course-note fact.")
    elif quiz_type == "mcq":
        question = clean_text(quiz.get("question"))
        if "which course-note term correctly completes this statement" in question.casefold():
            raise QuizValidationError("MCQ uses a mechanical blank-completion stem.")
        if not question.endswith("?") or not all(_mcq_option_quality(option) for option in quiz["options"]):
            raise QuizValidationError("MCQ question or options are not pedagogically usable.")
        correct_option = quiz["options"][int(quiz["correct_index"])]
        if not _phrase_in_notes(correct_option, context):
            raise QuizValidationError("The MCQ correct option is not grounded in the supplied notes.")
        explanation = clean_text(quiz.get("explanation"))
        if focus and not (
            clean_text(correct_option).casefold() in clean_text(focus.text).casefold()
            or clean_text(focus.text).casefold() in clean_text(correct_option).casefold()
            or jaccard(correct_option, focus.text) >= 0.28
            or (
                clean_text(correct_option).casefold() in explanation.casefold()
                and jaccard(explanation, focus.text) >= 0.28
            )
        ):
            raise QuizValidationError("The MCQ correct option does not test the selected focus fact.")
    elif quiz_type == "fill_blank":
        answer = clean_text(quiz.get("answer"))
        question = clean_text(quiz.get("question"))
        explanation = clean_text(quiz.get("explanation"))
        if not _instructional_fill_answer(answer):
            raise QuizValidationError(
                "The blank answer is a grammar/function word rather than a knowledge-bearing course term."
            )
        if not _fill_source_quality(focus.text if focus else explanation or question.replace("_____", answer)):
            raise QuizValidationError("The Fill Blank source is an outline item or incomplete statement.")
        if not _phrase_in_notes(answer, context):
            raise QuizValidationError("The blank answer is not an exact course-note term.")
        if question.count("_____") != 1:
            raise QuizValidationError("Fill Blank must contain exactly one visible blank.")
        restored = question.replace("_____", answer, 1)
        if not _phrase_in_notes(restored, context):
            raise QuizValidationError("The completed Fill Blank sentence is not grounded verbatim in the notes.")
        if explanation and jaccard(restored, explanation) < 0.82:
            raise QuizValidationError("The Fill Blank question, answer and explanation do not reconstruct the same fact.")
        if focus and jaccard(restored, focus.text) < 0.82:
            raise QuizValidationError("The Fill Blank does not test the selected focus fact.")
    elif quiz_type == "short_answer":
        key_points = [clean_text(item) for item in quiz.get("key_points", []) if clean_text(item)]
        if not (1 <= len(key_points) <= 4):
            raise QuizValidationError("Short Answer needs one to four explicit marking points.")
        if not all(_phrase_in_notes(point, context) for point in key_points):
            raise QuizValidationError("A Short Answer marking point is not grounded in the supplied notes.")
        if not all(_covers_short_answer_point(quiz.get("reference"), point) for point in key_points):
            raise QuizValidationError("The Short Answer reference omits a required course-note point.")
        if not all(_covers_short_answer_point(quiz.get("explanation"), point) for point in key_points):
            raise QuizValidationError("The Short Answer explanation omits a required course-note point.")
        if focus and not _short_answer_question_matches_evidence(quiz.get("question"), focus, key_points):
            raise QuizValidationError("The Short Answer question does not match its course-note rubric.")
    elif quiz_type == "matching":
        allowed = {
            (clean_text(item.get("term")).casefold(), clean_text(item.get("definition")))
            for item in term_candidates if _meaningful_term(item.get("term"))
        }
        for left_index, term in enumerate(quiz["left"]):
            definition = quiz["right"][quiz["correct_mapping"][left_index]]
            if not _meaningful_term(term, allow_lowercase=True):
                raise QuizValidationError("A Matching term is a heading or fragment.")
            supported_pair = any(
                term.casefold() == known_term and jaccard(definition, known_definition) >= 0.45
                for known_term, known_definition in allowed
            )
            supported_in_chunk = _phrase_in_notes(term, context) and _phrase_in_notes(definition, context)
            if not supported_pair and not supported_in_chunk:
                raise QuizValidationError("A Matching pair is not supported by the supplied course notes.")
    elif not _phrase_in_notes(quiz["question"], context):
        raise QuizValidationError("The statement is not sufficiently grounded in the selected notes.")


def _term_for_fact(focus: FocusFact, terms: list[dict[str, Any]], max_words: int = 5) -> dict[str, Any] | None:
    direct = [
        item for item in terms
        if _meaningful_term(item.get("term"))
        and clean_text(item.get("term")).casefold() in focus.text.casefold()
    ]
    direct.sort(key=lambda item: (jaccard(item.get("definition"), focus.text), len(clean_text(item.get("term")))), reverse=True)
    return next((item for item in direct if 1 <= len(clean_text(item.get("term")).split()) <= max_words), None)


def _course_terms(context: str) -> list[str]:
    candidates = re.findall(r"\b(?:[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9()/-]*){0,3}|[A-Z]{2,8})\b", context)
    output: list[str] = []
    for value in candidates:
        value = clean_text(value)
        if _meaningful_term(value) and value.casefold() not in {item.casefold() for item in output}:
            output.append(value)
    return output


def _bullet_groups(text: object) -> list[dict[str, Any]]:
    """Parse slide bullets, including inline ``Label: definition`` forms.

    PDF extraction often leaves a bullet label and the beginning of its
    definition on the same line, then puts the rest on unmarked wrapped lines.
    Earlier releases treated the whole first line as a label and discarded the
    continuation, which made coherent short-answer rubrics look incomplete.
    """
    raw_lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_detail = ""

    def flush_detail() -> None:
        nonlocal current_detail
        if current is not None and current_detail:
            current["details"].append(clean_text(current_detail).strip("•–- "))
        current_detail = ""

    def begin_group(body: str) -> tuple[dict[str, Any], str]:
        value = clean_text(body).strip("• ")
        # Explicit inline label/definition separators are common in lecture
        # slides: ``Spinning Motion: ...`` and ``Pull - customer ...``.
        match = re.match(r"^([^:]{1,90}?):\s*(.+)$", value)
        if not match:
            match = re.match(r"^(.{1,70}?)\s+(?:-|–)\s+(.+)$", value)
        if match:
            label = clean_text(match.group(1)).strip(" .:;- ")
            detail = clean_text(match.group(2)).strip(" .:;- ")
            if 1 <= len(label.split()) <= 10 and detail:
                return {"label": label, "details": []}, detail
        # A sentence-style application bullet is a point with content, not an
        # unexplained heading. Give it a neutral derived label so the question
        # can distinguish principles from application examples.
        if re.match(r"^(?:used|use) in\b", value, re.IGNORECASE):
            return {"label": "Applications", "details": []}, value
        return {"label": value, "details": []}, ""

    for raw in raw_lines:
        if raw.startswith("•"):
            flush_detail()
            if current is not None:
                groups.append(current)
            current, current_detail = begin_group(raw.lstrip("• "))
            continue
        if current is None:
            continue
        if re.match(r"^(?:https?://|example\b|q:)", raw, re.IGNORECASE):
            flush_detail()
            continue
        if raw.startswith(("–", "-")):
            if raw.strip() in {"-", "–"} and current_detail:
                current_detail = current_detail.rstrip() + "-"
                continue
            flush_detail()
            current_detail = clean_text(raw.lstrip("–- "))
        elif current_detail:
            # Wrapped continuation lines belong to the current definition. A
            # short capitalised line is only considered a new heading when the
            # previous text already forms a complete sentence.
            if (
                raw[:1].isupper()
                and len(raw.split()) <= 6
                and current_detail.rstrip().endswith((".", "!", "?"))
            ):
                flush_detail()
            else:
                current_detail = clean_text(f"{current_detail} {raw}")
        else:
            # Ignore free-standing headings that are not attached to a bullet.
            continue
    flush_detail()
    if current is not None:
        groups.append(current)
    return [group for group in groups if clean_text(group.get("label"))]


def _bullet_relations(text: object) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for group in _bullet_groups(text):
        definition = clean_text(" ".join(group.get("details") or []))
        label = clean_text(group.get("label"))
        if (_meaningful_term(label, allow_lowercase=True) or label.casefold().startswith("use as ")) and len(definition.split()) >= 4:
            relations.append({"label": label, "definition": definition})
    return relations


def _short_answer_focus(
    chunks: list[dict[str, Any]],
    history: dict | None = None,
    topic: str = "",
) -> FocusFact:
    """Prefer a finite, coherent relation group for a multi-point rubric.

    The generic focus selector is optimised for fact diversity. Short Answer has
    a different need: one slide/section should contain a complete two-to-four
    point rubric. This selector therefore favours compact label-definition groups
    before falling back to the normal fact selector.
    """
    used_facts = _used(history, "facts")
    used_chunks = _used(history, "chunks")
    if _definition_subject(topic):
        return select_focus(chunks, history, topic)
    candidates: list[tuple[float, FocusFact]] = []
    for chunk in chunks:
        groups = _bullet_groups(chunk.get("text"))
        relations = _bullet_relations(chunk.get("text"))
        incomplete_labels = [
            clean_text(group.get("label"))
            for group in groups
            if not group.get("details")
            and _meaningful_term(group.get("label"), allow_lowercase=True)
        ]
        # Do not turn four explained bullets out of a five-item slide into an
        # apparently complete four-point rubric. A standalone definition sentence
        # is not a list item, but a concise unexplained label is.
        if incomplete_labels or not (2 <= len(relations) <= 4):
            continue
        relation_body = " ".join(
            f"{clean_text(item.get('label'))} {clean_text(item.get('definition'))}"
            for item in relations
        )
        topic_score = lexical_score(topic, f"{chunk.get('section', '')} {relation_body}") if topic else 0.5
        if not chunk.get("topic_scoped") and topic and topic_score <= 0:
            continue

        facts = list(chunk.get("facts") or [])
        relation_foci: list[FocusFact] = []
        for relation in relations:
            definition = clean_text(relation.get("definition"))
            label = clean_text(relation.get("label"))
            ranked_facts: list[tuple[float, dict[str, Any]]] = []
            for fact in facts:
                fact_text = clean_text(fact.get("text"))
                if not fact_text:
                    continue
                overlap = max(jaccard(definition, fact_text), jaccard(f"{label} {definition}", fact_text))
                if definition.casefold() in fact_text.casefold() or fact_text.casefold() in definition.casefold():
                    overlap += 1.0
                ranked_facts.append((overlap, fact))
            ranked_facts.sort(
                key=lambda item: (
                    str(item[1].get("fact_key") or "") not in used_facts,
                    item[0],
                ),
                reverse=True,
            )
            if ranked_facts and ranked_facts[0][0] >= 0.35:
                fact = ranked_facts[0][1]
                fact_text = clean_text(fact.get("text"))
                fact_key = str(fact.get("fact_key") or f"F-{fingerprint(fact_text)}")
            else:
                fact_text = definition
                fact_key = f"F-{fingerprint(f'{label}: {definition}')}"
            relation_foci.append(FocusFact(fact_text, fact_key, chunk))

        chosen = next((item for item in relation_foci if item.fact_key not in used_facts), relation_foci[0])
        score = (
            2.0
            + 0.18 * len(relations)
            + 0.90 * topic_score
            + 0.45 * float(chunk.get("relevance", 0.0))
            + (0.22 if chosen.fact_key not in used_facts else 0.0)
            - (0.08 if str(chunk.get("chunk_id") or "") in used_chunks else 0.0)
        )
        candidates.append((score, chosen))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return select_focus(chunks, history, topic)


def _relation_noun_phrases(relations: list[dict[str, str]]) -> list[str]:
    output: list[str] = []
    patterns = (
        r"\b(?:search results?|tracking services?|area alarm|customer location|pre-defined activity)\b",
        r"\b(?:filter|pointer|definer/launcher)\b",
    )
    for relation in relations:
        body = f"{relation.get('label', '')} {relation.get('definition', '')}"
        for pattern in patterns:
            for match in re.findall(pattern, body, flags=re.IGNORECASE):
                value = clean_text(match)
                if value and value.casefold() not in {item.casefold() for item in output}:
                    output.append(value)
    return output


def _role_from_label(label: str) -> tuple[str, str]:
    value = clean_text(label)
    match = re.match(r"^use as (?:a|an)\s+(.+)$", value, re.IGNORECASE)
    if not match:
        return value, ""
    role = clean_text(match.group(1))
    target = ""
    if re.search(r"\s+for\s+", role, re.IGNORECASE):
        role, target = re.split(r"\s+for\s+", role, maxsplit=1, flags=re.IGNORECASE)
    return clean_text(role).strip('“”" '), clean_text(target).strip('“”" ')


def _display_label(value: object) -> str:
    text = clean_text(value).replace("“", "").replace("”", "")
    return text.strip(" .:;-")


def _shuffle_options(options: list[str], answer: str, seed: str) -> tuple[list[str], int]:
    distinct: list[str] = []
    for item in options:
        item = clean_text(item).strip("“”\"' ")
        if item and item.casefold() not in {value.casefold() for value in distinct}:
            distinct.append(item)
    if answer.casefold() not in {item.casefold() for item in distinct}:
        distinct.insert(0, answer)
    selected = [answer] + [item for item in distinct if item.casefold() != answer.casefold()][:3]
    if len(selected) != 4:
        raise QuizUnavailableError("The system could not construct four coherent MCQ options.")
    shift = int(hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:2], 16) % 4
    selected = selected[shift:] + selected[:shift]
    return selected, selected.index(answer)


def _detail_quality(value: object) -> bool:
    text = clean_text(value).strip("•–- ")
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text)
    return (
        2 <= len(words) <= 32
        and 8 <= len(text) <= 220
        and not text.endswith((":", "?"))
        and _balanced_delimiters(text)
        and _balanced_quotes(text)
        and not any(re.search(pattern, text.casefold()) for pattern in _BAD_FRAGMENT_PATTERNS)
    )


def _relation_mcq(focus: FocusFact, chunks: list[dict[str, Any]], topic: str) -> dict[str, Any] | None:
    focus_groups = _bullet_groups(focus.chunk.get("text"))
    if not focus_groups:
        return None

    def overlap(group: dict[str, Any]) -> float:
        return max(
            [jaccard(focus.text, group.get("label"))]
            + [jaccard(focus.text, detail) for detail in group.get("details") or []]
        )

    chosen = max(focus_groups, key=overlap)
    chosen_label = clean_text(chosen.get("label"))
    # Sentence bullets such as "Multipath is the effect..." should be handled
    # by the definition/fact MCQ generators, not treated as term labels.
    if not (_meaningful_term(chosen_label, allow_lowercase=True) or chosen_label.casefold().startswith("use as ")):
        return None
    if overlap(chosen) < 0.18:
        return None
    details = [clean_text(item).rstrip(".") for item in chosen.get("details") or [] if _detail_quality(item)]
    if not details:
        return None
    ranked_details = sorted(details, key=lambda item: jaccard(focus.text, item), reverse=True)
    detail_overlap = jaccard(focus.text, ranked_details[0])
    answer = ranked_details[0]
    if not _mcq_option_quality(answer):
        return None
    label = _display_label(chosen.get("label"))

    distractors: list[str] = []
    # Sibling bullet details are strongest distractors because they are from the
    # same slide and grammatical level, but belong to another labelled concept.
    for group in focus_groups:
        if group is chosen:
            continue
        distractors.extend(
            clean_text(item).rstrip(".")
            for item in group.get("details") or []
            if _detail_quality(item)
        )
    # Add related course-note details from other retrieved pages only when more
    # options are needed. They remain source-grounded and topic-relevant.
    for chunk in chunks:
        if chunk.get("chunk_id") == focus.chunk.get("chunk_id"):
            continue
        if not _fact_matches_topic(topic, chunk.get("text"), chunk):
            continue
        for group in _bullet_groups(chunk.get("text")):
            distractors.extend(
                clean_text(item).rstrip(".")
                for item in group.get("details") or []
                if _detail_quality(item)
            )

    try:
        options, correct_index = _shuffle_options(
            [answer, *(item for item in distractors if _mcq_option_quality(item))],
            answer, focus.fact_key
        )
    except QuizUnavailableError:
        return None

    if detail_overlap >= 0.18:
        question = f"Which item is listed under “{label}” in the course notes?"
    else:
        question = f"Which function is associated with “{label}” in the course notes?"
    return {
        "type": "mcq",
        "question": question,
        "options": options,
        "correct_index": correct_index,
        "explanation": f"{label}: {answer}",
    }


def _term_definition_mcq(focus: FocusFact, chunks: list[dict[str, Any]], terms: list[dict[str, Any]], topic: str) -> dict[str, Any] | None:
    context = _all_context(chunks)
    valid = [
        item for item in terms
        if _meaningful_term(item.get("term"))
        and _usable_definition(item.get("definition"))
        and _phrase_in_notes(item.get("definition"), context)
        and float(item.get("relevance", 0.0)) > 0
    ]
    if not valid:
        return None
    ranked = sorted(
        valid,
        key=lambda item: jaccard(focus.text, item.get("definition")),
        reverse=True,
    )
    chosen = ranked[0]
    relation_score = jaccard(focus.text, chosen.get("definition"))
    if relation_score < 0.28:
        return None

    term = clean_text(chosen.get("term"))
    answer = _polish_sentence(chosen.get("definition"))
    if not _mcq_option_quality(answer):
        return None
    category = clean_text(chosen.get("category"))
    candidates = [
        _polish_sentence(item.get("definition")) for item in valid
        if clean_text(item.get("definition")).casefold() != answer.casefold()
        and (not category or clean_text(item.get("category")) == category)
        and _mcq_option_quality(item.get("definition"))
    ]
    candidates.extend(
        _polish_sentence(item.get("definition")) for item in valid
        if clean_text(item.get("definition")).casefold() != answer.casefold()
        and _mcq_option_quality(item.get("definition"))
    )
    try:
        options, correct_index = _shuffle_options([answer, *candidates], answer, focus.fact_key)
    except QuizUnavailableError:
        return None
    return {
        "type": "mcq",
        "question": (
            "Which statement best describes a lamp in building services?"
            if re.fullmatch(r"In building services,\s+the lamp", term, re.IGNORECASE)
            else f"Which statement best describes {_matching_term(term) or term} according to the course notes?"
        ),
        "options": options,
        "correct_index": correct_index,
        "explanation": f"{term}: {clean_text(chosen.get('definition'))}",
    }


def _mcq_statement(value: object) -> bool:
    text = clean_text(value).strip("•–- ")
    lowered = text.casefold()
    return (
        _usable_fact(text)
        and not lowered.startswith(("to ", "use as "))
        and _balanced_quotes(text)
        and not text.endswith((":", "?"))
    )


def _fact_mcq(focus: FocusFact, chunks: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    answer = _polish_sentence(focus.text)
    if not _mcq_statement(answer) or not _mcq_option_quality(answer):
        raise QuizUnavailableError("The selected notes do not contain a readable grounded MCQ answer.")
    section = clean_text(focus.chunk.get("section")) or "the selected section"
    candidates: list[str] = []
    backup_candidates: list[str] = []
    for chunk in chunks:
        for fact in chunk.get("facts", []):
            text = _polish_sentence(fact.get("text"))
            if not _mcq_statement(text) or not _mcq_option_quality(text) or text.casefold() == answer.casefold():
                continue
            # When no topic was supplied, keep the original section isolation
            # so a section-name question still has exactly one supported option.
            if not topic and clean_text(chunk.get("section")).casefold() == section.casefold():
                continue
            if topic and _fact_matches_topic(topic, text, chunk):
                # Topic search is intentionally broad (one shared word can be
                # enough), so a retrieved lighting fact can still be a safe
                # distractor for "definition of lamp". Keep low-overlap facts as
                # backup rather than rejecting the whole MCQ.
                if jaccard(text, answer) < 0.18:
                    backup_candidates.append(text)
                continue
            candidates.append(text)
    candidates.extend(item for item in backup_candidates if item.casefold() not in {value.casefold() for value in candidates})
    options, correct_index = _shuffle_options([answer, *candidates], answer, focus.fact_key)
    if topic:
        question = f"Which statement best describes “{_display_label(topic)}” according to the course notes?"
    else:
        question = f"Which statement is presented in the course-note section “{_display_label(section)}”?"
    return {
        "type": "mcq", "question": question, "options": options,
        "correct_index": correct_index, "explanation": answer,
    }


def _polish_sentence(value: object) -> str:
    text = clean_text(value).strip("•–- ")
    text = re.sub(r"\.{2,}$", ".", text)
    if text and text[:1].islower():
        text = text[:1].upper() + text[1:]
    return text


def _true_false_target(topic: str = "", variation_seed: str = "", attempt_index: int = 0) -> int:
    """Alternate the requested answer across *generated attempts* in one session.

    The session/topic hash chooses only the first answer.  The number of already
    generated questions comes from persisted history, so reloads do not reset
    the sequence and consecutive retries cannot all inherit the same answer.
    A safe True item is allowed when no defensible False can be constructed.
    """
    seed = f"{variation_seed}|{clean_text(topic).casefold()}|true-false"
    first = int(hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:2], 16) % 2
    return (first + max(0, int(attempt_index))) % 2


def _false_variant(statement: str, seed: str) -> str | None:
    """Create one auditable contradiction, or return None when unsafe.

    The previous last-resort wording ("the notes do not state that...") was a
    meta-claim rather than a contradiction of the fact and also failed its own
    grounding validator for short statements such as "Based on the CDMA
    principle."  v8.0.10 never emits that form.
    """
    text = _polish_sentence(statement)

    number_matches = list(re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)?", text))
    if number_matches:
        match = number_matches[int(hashlib.sha256(seed.encode()).hexdigest()[:2], 16) % len(number_matches)]
        original = match.group(0)
        if "." in original:
            decimals = len(original.split(".", 1)[1])
            replacement = f"{float(original) + (10 ** -decimals):.{decimals}f}"
        else:
            replacement = str(int(original) + 1)
        return f"{text[:match.start()]}{replacement}{text[match.end():]}"

    removed = re.sub(
        r"\b(is|are|was|were|can|could|will|would|should|has|have|had|does|do|did)\s+not\b",
        r"\1",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if removed != text:
        return removed

    inserted = re.sub(
        r"\b(is|are|was|were|can|could|will|would|should|has|have|had)\b",
        r"\1 not",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if inserted != text:
        return inserted

    antonyms = {
        "more": "less", "less": "more", "higher": "lower", "lower": "higher",
        "increase": "decrease", "increases": "decreases", "increased": "decreased",
        "decrease": "increase", "decreases": "increases", "decreased": "increased",
        "before": "after", "after": "before", "inside": "outside", "outside": "inside",
        "minimum": "maximum", "maximum": "minimum",
        "minimize": "maximize", "maximize": "minimize",
        "minimizes": "maximizes", "maximizes": "minimizes",
        "minimise": "maximise", "maximise": "minimise",
    }
    words = list(re.finditer(r"\b[A-Za-z]+\b", text))
    replaceable = [match for match in words if match.group(0).casefold() in antonyms]
    if replaceable:
        match = replaceable[int(hashlib.sha256((seed + "-antonym").encode()).hexdigest()[:2], 16) % len(replaceable)]
        replacement = antonyms[match.group(0).casefold()]
        if match.group(0)[:1].isupper():
            replacement = replacement[:1].upper() + replacement[1:]
        return f"{text[:match.start()]}{replacement}{text[match.end():]}"
    return None


def _bounded_false_contradiction(question: object, explanation: object) -> bool:
    """Check that exactly one bounded detail changed from the grounded fact."""
    false_text = clean_text(question).casefold()
    true_text = clean_text(explanation).casefold()
    if not false_text or false_text == true_text:
        return False

    token_re = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?")
    false_tokens = token_re.findall(false_text)
    true_tokens = token_re.findall(true_text)

    # One inserted/removed negation.
    if len(false_tokens) == len(true_tokens) + 1:
        for index, token in enumerate(false_tokens):
            if token == "not" and false_tokens[:index] + false_tokens[index + 1:] == true_tokens:
                return True
    if len(true_tokens) == len(false_tokens) + 1:
        for index, token in enumerate(true_tokens):
            if token == "not" and true_tokens[:index] + true_tokens[index + 1:] == false_tokens:
                return True

    if len(false_tokens) != len(true_tokens):
        return False
    differences = [(left, right) for left, right in zip(false_tokens, true_tokens) if left != right]
    if len(differences) != 1:
        return False
    left, right = differences[0]
    if re.fullmatch(r"\d+(?:\.\d+)?", left) and re.fullmatch(r"\d+(?:\.\d+)?", right):
        return True
    antonym_pairs = {
        frozenset(("more", "less")), frozenset(("higher", "lower")),
        frozenset(("increase", "decrease")), frozenset(("increases", "decreases")),
        frozenset(("increased", "decreased")), frozenset(("before", "after")),
        frozenset(("inside", "outside")),
        frozenset(("minimum", "maximum")),
        frozenset(("minimize", "maximize")), frozenset(("minimizes", "maximizes")),
        frozenset(("minimise", "maximise")),
    }
    return frozenset((left, right)) in antonym_pairs


def _instructional_fill_answer(value: object) -> bool:
    """Return True only for a knowledge-bearing Fill Blank answer.

    The answer may be a concise course term, component, quantity, named role or
    fact-bearing property.  A preposition, article, connector, auxiliary verb or
    other function word is never sufficient even though it appears in the PDF.
    """
    item = clean_text(value).strip("“”\"' ,.;:-")
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9()/%+.-]*", item)
    if not (1 <= len(words) <= 3):
        return False
    lowered = [word.casefold() for word in words]
    if not item or all(word in _FILL_BLANK_FUNCTION_WORDS for word in lowered):
        return False
    if len(words) == 1 and lowered[0] in _FILL_NON_TECHNICAL:
        return False
    if lowered[0] in _FILL_BLANK_FUNCTION_WORDS or lowered[-1] in _FILL_BLANK_FUNCTION_WORDS:
        return False
    if any(word in {"that", "which", "who", "where", "when", "because", "and", "or"} for word in lowered):
        return False
    if any(word in {"holds", "protects", "produces", "emits", "changes", "obtains", "uses", "making"} for word in lowered):
        return False
    # A one-letter ordinary word is never a course concept.  Acronyms, numbers
    # and units remain valid (for example GPS, 24 hours, 10-100 m).
    if len(words) == 1 and words[0].isalpha() and not words[0].isupper() and len(words[0]) < 3:
        return False
    return True


def _clean_fill_candidate(value: object) -> str:
    item = clean_text(value).strip("“”\"' ,.;:-")
    words = item.split()
    removable_leads = {"appropriate", "specific", "same", "other", "local", "major", "selected", "relevant"}
    removable_tails = {"only", "also", "respectively"}
    while len(words) > 1 and words[0].casefold() in removable_leads:
        words.pop(0)
    while len(words) > 1 and words[-1].casefold() in removable_tails:
        words.pop()
    item = " ".join(words)
    return item if _instructional_fill_answer(item) else ""


def _fill_blank_candidates(focus_text: str, topic: str = "") -> list[str]:
    candidates: list[str] = []
    for quoted in re.findall(r"[‘’'\"“”]([^‘’'\"“”]{2,45})[‘’'\"“”]", focus_text):
        quoted = clean_text(quoted)
        if 1 <= len(quoted.split()) <= 3 and quoted.casefold() not in _GENERIC_TERMS:
            candidates.append(quoted)
    for token in re.findall(r"\b[A-Za-z][A-Za-z-]*/[A-Za-z][A-Za-z/-]*\b", focus_text):
        if len(token.split()) <= 3:
            candidates.append(clean_text(token))
    candidates.extend(term for term in _course_terms(focus_text) if len(term.split()) <= 3)

    phrase_pattern = re.compile(
        r"\b(?:the|a|an)\s+((?:[A-Za-z][A-Za-z-]*\s+){0,2}[A-Za-z][A-Za-z-]*)"
        r"(?=\s+(?:to|for|from|with|by|through|of|and|or|is|are|was|were|has|have|had)\b|[.,;:]|$)",
        re.IGNORECASE,
    )
    phrase_candidates = [_clean_fill_candidate(match.group(1)) for match in phrase_pattern.finditer(focus_text)]
    candidates.extend(item for item in phrase_candidates if item)
    for match in re.finditer(r"\busing\s+([A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*){0,2})(?=\s+(?:of|for|to|with|by)\b|[.,;:]|$)", focus_text, re.IGNORECASE):
        item = _clean_fill_candidate(match.group(1))
        if item:
            candidates.append(item)

    # Extract fact-bearing complements that simple title-case/course-term rules
    # miss.  This catches useful items such as ``receiver`` in ``requires a
    # receiver`` and ``passive`` in ``the system is passive`` while still
    # rejecting the introductory preposition ``For``.
    pedagogical_patterns = (
        r"\brequires?\s+(?:a|an|the)?\s*([A-Za-z][A-Za-z-]{2,}(?:\s+[A-Za-z][A-Za-z-]{2,}){0,2})(?=\s+(?:only|without|with|for|to|from|by|through|and|or|[.,;:]|$))",
        r"\b(?:is|are|was|were|becomes?|means?)\s+(?:a|an|the)?\s*([A-Za-z][A-Za-z-]{2,}(?:\s+[A-Za-z][A-Za-z-]{2,}){0,2})(?=\s+(?:and|or|but|with|without|for|to|from|by|through|[.,;:]|$))",
        r"\b(?:a|an|the)\s+([A-Za-z][A-Za-z-]{3,})(?=\s+(?:only|without|with|for|to|from|by|through|of|and|or|[.,;:]|$))",
    )
    for pattern in pedagogical_patterns:
        for match in re.finditer(pattern, focus_text, re.IGNORECASE):
            item = _clean_fill_candidate(match.group(1))
            if item:
                candidates.append(item)

    topic_text = clean_text(topic)
    if topic_text and topic_text.casefold() in focus_text.casefold() and len(topic_text.split()) <= 3:
        candidates.append(topic_text)
    output: list[str] = []
    for item in candidates:
        item = _clean_fill_candidate(item)
        if item and item.casefold() not in {value.casefold() for value in output}:
            output.append(item)
    return output


def _fill_candidate_score(
    candidate: str,
    focus_text: str,
    topic: str = "",
    known_terms: set[str] | None = None,
) -> tuple[int, int, int]:
    """Rank candidates by educational value, not merely source order."""
    value = _clean_fill_candidate(candidate)
    if not value or value.casefold() not in focus_text.casefold():
        return (-10_000, 0, 0)
    lowered = value.casefold()
    score = 0
    if lowered in (known_terms or set()):
        score += 6
    if value.isupper() and 2 <= len(value) <= 10:
        score += 3
    escaped = re.escape(value)
    if re.search(rf"\b(?:a|an|the)\s+{escaped}\s+(?:is|are|means|refers)\b", focus_text, re.IGNORECASE):
        score += 10
    if re.search(rf"\busing\s+{escaped}\b", focus_text, re.IGNORECASE):
        score += 8
    if 2 <= len(value.split()) <= 3:
        score += 2
    if re.search(r"(?:tion|ment|ity|ance|ence|ology|graphy|meter|ceiver|mitter)\b", lowered):
        score += 3
    escaped = re.escape(value)
    if re.search(rf"\brequires?\s+(?:a|an|the)?\s*{escaped}\b", focus_text, re.IGNORECASE):
        score += 10
    elif re.search(rf"\b(?:is|are|was|were|means?)\s+(?:a|an|the)?\s*{escaped}\b", focus_text, re.IGNORECASE):
        score += 5
    if clean_text(topic) and lowered == clean_text(topic).casefold():
        # A broad session topic is acceptable only when no more specific
        # course concept exists in the selected fact.
        score -= 5
    return (score, len(value.split()), len(value))


def _fill_source_quality(value: object) -> bool:
    """Require a standalone instructional proposition, not a PPT outline row.

    Deliberately narrower than _usable_fact: a chapter heading can be useful
    retrieval metadata but it must never be turned into a cloze question.
    """
    statement = _polish_sentence(value).strip("•– ")
    if not _usable_fact(statement) or not _readable_lecture_text(statement):
        return False
    if re.match(r"^(?:\d{1,2}\s*[.)]|[A-Z]\s*[.)])\s*", statement):
        return False
    if re.search(r"(?:^|\s)\d{1,2}\s*[.)]\s*(?:$|[A-Z][a-z])", statement):
        return False
    if re.match(r"^(?:summary|contents|outline|syllabus|lecture contents|learning objectives|exercise)\b", statement, re.I):
        return False
    if statement.endswith(":") or statement.count(" _____ "):
        return False
    # Course material may contain abbreviations and page references, but a
    # summary-list item ending in another enumeration number is unusable.
    if re.search(r"\b\d+\.\s*$", statement):
        return False
    return True


def _fill_blank_answer(focus: FocusFact, terms: list[dict[str, Any]], topic: str = "") -> str:
    if not _fill_source_quality(focus.text):
        return ""
    direct_terms = [
        _clean_fill_candidate(item.get("term"))
        for item in terms
        if _meaningful_term(item.get("term"))
        and clean_text(item.get("term")).casefold() in focus.text.casefold()
    ]
    direct_terms = [item for item in direct_terms if item]
    known = {item.casefold() for item in direct_terms}
    candidates = direct_terms + _fill_blank_candidates(focus.text, topic)
    unique: list[str] = []
    for item in candidates:
        if item and item.casefold() not in {value.casefold() for value in unique}:
            unique.append(item)
    unique.sort(
        key=lambda item: _fill_candidate_score(item, focus.text, topic, known),
        reverse=True,
    )
    answer = unique[0] if unique and _fill_candidate_score(unique[0], focus.text, topic, known)[0] > -10_000 else ""
    return answer if answer and answer.casefold() in focus.text.casefold() else ""


def _blank_phrase(text: str, answer: str) -> str:
    pattern = re.escape(answer)
    if answer[:1].isalnum():
        pattern = rf"(?<!\w){pattern}"
    if answer[-1:].isalnum():
        pattern = rf"{pattern}(?!\w)"
    return re.sub(pattern, "_____", text, count=1, flags=re.IGNORECASE)


def _fill_blank_focus_candidates(
    chunks: list[dict[str, Any]],
    history: dict | None,
    topic: str,
) -> list[FocusFact]:
    """Return unused topic-relevant facts first, followed by safe reuse."""
    used_facts = _used(history, "facts")
    unused: list[FocusFact] = []
    reused: list[FocusFact] = []
    seen: set[str] = set()
    for chunk in chunks:
        for fact in list(chunk.get("facts") or []):
            text = clean_text(fact.get("text"))
            if not _fill_source_quality(text) or not _fact_matches_topic(topic, text, chunk):
                continue
            subject = _definition_subject(topic)
            if subject and not set(tokens(subject)) <= set(tokens(text)):
                continue
            key = str(fact.get("fact_key") or f"F-{fingerprint(text)}")
            if key in seen:
                continue
            seen.add(key)
            candidate = FocusFact(text, key, chunk)
            (reused if key in used_facts else unused).append(candidate)
    return unused + reused


def _alternate_fill_blank_focus(
    current: FocusFact,
    chunks: list[dict[str, Any]],
    terms: list[dict[str, Any]],
    history: dict | None,
    topic: str,
) -> FocusFact:
    for candidate in _fill_blank_focus_candidates(chunks, history, topic):
        if candidate.fact_key == current.fact_key:
            continue
        answer = _fill_blank_answer(candidate, terms, topic)
        if answer and _blank_phrase(candidate.text, answer) != candidate.text:
            return candidate
    raise QuizUnavailableError("The relevant notes do not contain a safe distinctive term for a Fill Blank question.")


def _matching_term(value: object) -> str:
    """A displayed term must be ONE named concept, not an outline or list."""
    term = clean_text(value).strip("•–-,:;. ")
    term = re.sub(r"^In building services,\s+(?:the|a|an)\s+", "", term, flags=re.I)
    term = re.sub(r"^(?:a|an|the)\s+", "", term, flags=re.I)
    if re.search(r"[,;]", term) or re.search(r"\s+and\s+", term, flags=re.I):
        return ""
    if re.search(r"\b(?:what|how|why|if|this|that|which|when)\b", term, re.I):
        return ""
    if len(term.split()) > 5 or not _meaningful_term(term, allow_lowercase=True):
        return ""
    if not _readable_lecture_text(term) or re.search(r"[\uE000-\uF8FF\uFFFD\u25A0-\u25FF]", term):
        return ""
    return term


def _matching_neighborhood_pairs(
    chunks: list[dict[str, Any]], focus: FocusFact, topic: str
) -> list[dict[str, Any]]:
    """Extract a small set of explicitly supported, neighboring definitions.

    Avoid treating all slide bullets and course-heading text as glossary
    entries. The limited patterns here are intentionally conservative.
    """
    output: list[dict[str, Any]] = []
    for chunk in chunks:
        if focus.chunk.get("document_id") and chunk.get("document_id") != focus.chunk.get("document_id"):
            continue
        if focus.chunk.get("page") and chunk.get("page") and abs(int(chunk["page"]) - int(focus.chunk["page"])) > 5:
            continue
        body = clean_text(chunk.get("text"))
        category = clean_text(chunk.get("chapter")) or clean_text(topic) or "Course-note concepts"
        # A) Exact definitions ("A loss function states ...", "The rule combines ...").
        for fact in chunk.get("facts") or []:
            sentence = clean_text(fact.get("text")).rstrip(" .") + "."
            if not (_readable_lecture_text(sentence) and 20 <= len(sentence) <= 250):
                continue
            m = re.match(
                r"^(?:a|an|the)\s+([A-Za-z][A-Za-z -]{2,55}?)\s+"
                r"(?:is|are|means|states|describes|combines|provides|refers to)\s+(.{12,200})$",
                sentence, flags=re.I,
            )
            if m:
                term = _matching_term(m.group(1))
                definition = clean_text(m.group(2)).rstrip(" .")
                if (term and not definition.casefold().startswith(("actually ", "already ", "sometimes ", "often "))
                        and not re.search(r"\b\d+[.)](?:\s|[A-Z])", definition)
                        and _usable_definition(definition) and _readable_lecture_text(definition)):
                    output.append({"term": term, "definition": definition, "category": category, **{
                        key: chunk.get(key) for key in ("page", "source_file", "chunk_id", "knowledge_key", "relevance")
                    }})
        # B) Two explicit relationships in the relevant Bayes lecture. These
        #    patterns match readable words actually present in the uploaded
        #    material, not reconstructed equations or unsupported definitions.
        if re.search(r"weighted average/conditional expected value is the conditional risk", body, re.I) and re.search(r"loss based on the\s+posterior probability", body, re.I):
            output.append({"term": "conditional risk",
                           "definition": "the weighted average of the loss based on the posterior probability for a given x",
                           "category": category, **{key:chunk.get(key) for key in ("page", "source_file", "chunk_id", "knowledge_key", "relevance")}})
        if re.search(r"loss-function is chosen as the error-rate\s*\(0-1 loss\).*?called\s+minimum error-rate classifier", body, re.I):
            output.append({"term": "minimum error-rate classifier",
                           "definition": "the loss-function is chosen as the error-rate (0-1 loss)",
                           "category": category, **{key:chunk.get(key) for key in ("page", "source_file", "chunk_id", "knowledge_key", "relevance")}})
    return output


def local_quiz(
    quiz_type: str,
    focus: FocusFact,
    chunks: list[dict[str, Any]],
    terms: list[dict[str, Any]],
    topic: str = "",
    true_false_correct_index: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if quiz_type == "true_false":
        explanation = _polish_sentence(focus.text)
        correct_index = (
            int(true_false_correct_index)
            if true_false_correct_index in {0, 1}
            else _true_false_target(topic)
        )
        statement = explanation if correct_index == 0 else _false_variant(explanation, focus.fact_key)
        if not statement:
            raise QuizUnavailableError("The selected fact cannot be changed into a safe bounded False statement.")
        return {
            "type": quiz_type, "question": statement, "options": ["True", "False"], "correct_index": correct_index,
            "explanation": explanation,
        }, [citation(focus.chunk, focus.text)]

    if quiz_type == "mcq":
        quiz = _relation_mcq(focus, chunks, topic) or _term_definition_mcq(focus, chunks, terms, topic)
        if quiz is None:
            quiz = _fact_mcq(focus, chunks, topic)
        return quiz, [citation(focus.chunk, quiz.get("explanation") or focus.text)]

    if quiz_type == "fill_blank":
        answer = _fill_blank_answer(focus, terms, topic)
        if not answer:
            raise QuizUnavailableError("The notes do not contain a safe distinctive term for fill_blank fallback.")
        statement = _polish_sentence(focus.text)
        blanked = _blank_phrase(statement, answer)
        if blanked == statement:
            raise QuizUnavailableError("A grounded blank could not be formed from the selected fact.")
        return {"type": quiz_type, "question": blanked, "answer": answer, "explanation": statement}, [citation(focus.chunk, focus.text)]

    if quiz_type == "matching":
        # Preserve an existing, explicitly labelled topic-category glossary.
        # GPS Components spans several slides; those three indexed pairs are
        # more coherent than ad-hoc pairs near the top-ranked GPS example.
        glossary: dict[str, list[dict[str, Any]]] = {}
        for source in terms:
            category = clean_text(source.get("category"))
            term = _matching_term(source.get("term"))
            definition = clean_text(source.get("definition")).rstrip(" .") + "."
            if (lexical_score(topic, category) < 0.65 or not term
                    or not _usable_definition(definition) or not _readable_lecture_text(definition)):
                continue
            group = glossary.setdefault(category, [])
            if term.casefold() not in {item["term"].casefold() for item in group}:
                group.append({**source, "term": term, "definition": definition})
        ranked_glossaries = sorted(
            (items for items in glossary.values() if len(items) >= 3),
            key=lambda items:(lexical_score(topic, items[0].get("category")), len(items)), reverse=True,
        )
        if ranked_glossaries:
            chosen = ranked_glossaries[0][:4]
            quiz = normalize_quiz({"type":"matching", "question":"Match each course-note term with its supported definition.",
                                   "pairs":[[item["term"],item["definition"]] for item in chosen]},
                                  "matching", focus.fact_key)
            citations = [{"reference_id": i, "source_file": item.get("source_file", ""),
                          "chapter":"", "section":item.get("category", ""),
                          "page":item.get("page"), "excerpt":item["definition"],
                          "chunk_id":item.get("chunk_id", ""),
                          "knowledge_key":item.get("knowledge_key", "")}
                         for i,item in enumerate(chosen,1)]
            return quiz,citations
        context = _all_context(chunks)
        source_file = clean_text(focus.chunk.get("source_file"))
        anchor_page = focus.chunk.get("page")
        pool: list[dict[str, Any]] = []
        for item in terms:
            term = _matching_term(item.get("term"))
            definition = clean_text(item.get("definition")).rstrip(" .") + "."
            if not term or not _usable_definition(definition) or not _readable_lecture_text(definition):
                continue
            if source_file and clean_text(item.get("source_file")) != source_file:
                continue
            if anchor_page and item.get("page") and abs(int(item["page"]) - int(anchor_page)) > 5:
                continue
            if not (_phrase_in_notes(term, context) and _phrase_in_notes(definition, context)):
                continue
            pool.append({**item, "term": term, "definition": definition})
        for chunk in chunks:
            if source_file and clean_text(chunk.get("source_file")) != source_file:
                continue
            if anchor_page and chunk.get("page") and abs(int(chunk["page"]) - int(anchor_page)) > 5:
                continue
            for relation in _bullet_relations(chunk.get("text")):
                label = clean_text(relation.get("label"))
                if label.casefold().startswith("use as "):
                    label = _role_from_label(label)[0]
                term = _matching_term(label)
                definition = clean_text(relation.get("definition"))
                if not term or not _usable_definition(definition) or not _readable_lecture_text(definition):
                    continue
                pool.append({"term": term, "definition": definition, "category": chunk.get("section") or topic,
                             **{key: chunk.get(key) for key in ("page", "source_file", "chunk_id", "knowledge_key", "relevance")}})
        pool.extend(_matching_neighborhood_pairs(chunks, focus, topic))
        ranked: list[tuple[float, dict[str, Any]]] = []
        seen_terms: set[str] = set()
        for item in pool:
            term = _matching_term(item.get("term"))
            definition = clean_text(item.get("definition")).rstrip(" .") + "."
            if not term or not _usable_definition(definition) or not _readable_lecture_text(definition):
                continue
            if term.casefold() in seen_terms:
                continue
            if not (_phrase_in_notes(term, context) and _phrase_in_notes(definition, context)):
                continue
            seen_terms.add(term.casefold())
            proximity = 0.0 if not anchor_page or item.get("page") is None else max(0.0, 1.0 - abs(int(item["page"]) - int(anchor_page))/6)
            relevance = lexical_score(topic, f"{term} {definition}") if topic else 0.5
            ranked.append((2.5*relevance + proximity + 0.25 * float(item.get("relevance") or 0.0), item))
        # A genuine glossary page should stay self-contained. Look at nearby
        # pages only if fewer than three usable concepts occur on the anchor.
        if anchor_page:
            same_page = [entry for entry in ranked if entry[1].get("page") == anchor_page]
            if len({entry[1]["term"].casefold() for entry in same_page}) >= 3:
                ranked = same_page
        ranked.sort(key=lambda x:x[0], reverse=True)
        chosen: list[dict[str, Any]] = []
        for _score, item in ranked:
            if any(jaccard(item["definition"], old["definition"]) >= 0.78 for old in chosen):
                continue
            chosen.append(item)
            if len(chosen) >= 4:
                break
        if len(chosen) < 3:
            raise QuizUnavailableError("The selected notes do not contain three distinct, supported term-definition pairs for this topic.")
        raw = {"type": "matching", "question": "Match each course-note term with its supported definition.",
               "pairs": [[item["term"], item["definition"]] for item in chosen]}
        quiz = normalize_quiz(raw, "matching", focus.fact_key)
        citations = [{"reference_id": index, "source_file": item.get("source_file", ""), "chapter": "",
                      "section": item.get("category", ""), "page": item.get("page"),
                      "excerpt": clean_text(item.get("definition")),
                      "chunk_id": item.get("chunk_id", ""), "knowledge_key": item.get("knowledge_key", "")}
                     for index,item in enumerate(chosen,1)]
        return quiz, citations

    if quiz_type == "short_answer":
        seed = {"type": "short_answer", "question": "", "reference": focus.text, "explanation": focus.text}
        return _prepare_short_answer_quiz(seed, focus, chunks, topic)
    raise QuizUnavailableError("Unsupported exercise type.")


def generation_prompt(
    quiz_type: str,
    topic: str,
    focus: FocusFact,
    chunks: list[dict[str, Any]],
    terms: list[dict[str, Any]],
    history: dict,
    true_false_correct_index: int | None = None,
) -> str:
    tf_index = 0 if true_false_correct_index not in {0, 1} else int(true_false_correct_index)
    schemas = {
        "true_false": f'{{"type":"true_false","question":"...","options":["True","False"],"correct_index":{tf_index},"explanation":"..."}}',
        "mcq": '{"type":"mcq","question":"...","options":["...","...","...","..."],"correct_index":0,"explanation":"..."}',
        "matching": '{"type":"matching","question":"...","pairs":[{"term":"...","definition":"..."},{"term":"...","definition":"..."},{"term":"...","definition":"..."}],"explanation":"..."}',
        "fill_blank": '{"type":"fill_blank","question":"... _____ ...","answer":"one to three words","explanation":"..."}',
        "short_answer": '{"type":"short_answer","question":"...","key_points":["..."],"reference":"complete grounded answer covering every key point","explanation":"complete note-based explanation covering every key point"}',
    }
    # Keep external prompts small. The focus passage is always first, followed by
    # at most three nearby retrieved chunks. This materially reduces Coze latency
    # without weakening the backend grounding check.
    prompt_chunks: list[dict[str, Any]] = [focus.chunk]
    for item in chunks:
        if clean_text(item.get("chunk_id")) == clean_text(focus.chunk.get("chunk_id")):
            continue
        prompt_chunks.append(item)
        if len(prompt_chunks) >= 4:
            break
    references = "\n\n".join(
        f"[Reference {index}] File={item.get('source_file')} | Section={item.get('section')} | Page={item.get('page')} | Chunk={item.get('chunk_id')}\n{clean_text(item.get('text'))[:800]}"
        for index, item in enumerate(prompt_chunks, 1)
    )
    allowed_terms = "\n".join(
        f"- {item.get('term')} :: {item.get('definition')} :: category={item.get('category')}"
        for item in terms[:12] if _meaningful_term(item.get("term"))
    )
    type_rules = {
        "mcq": (
            "Ask a natural knowledge question about a concept, function, application, relationship, or definition. "
            "Do not turn a sentence into a blank and do not use the stem 'Which course-note term correctly completes this statement?'. "
            "All four options must be meaningful and parallel in grammatical form. Never use headings, labels, isolated generic words, "
            "questions, or fragments such as 'Class discussions: What' or 'Use'. Exactly one option must answer the question. "
            "If the source contains damaged equation text, use a concise plain-English relationship directly supported by the "
            "readable references; never copy broken mathematical notation or guess missing symbols. "
            "Do not include an option that is incomplete, an outline title, or longer than 55 words."
        ),
        "matching": "Use only complete, meaningful same-category term-definition pairs. Do not use headings or sentence fragments as terms.",
        "fill_blank": (
            "Blank one knowledge-bearing technical concept, component, measurement, named role, or crucial property of at most three words. "
            "Never blank an article, preposition, conjunction, pronoun, auxiliary verb, introductory connector, or other grammar/function word "
            "such as for, to, the, of, in, and, is, or only. The item must assess course knowledge rather than English grammar. "
            "Do not blank the broad student topic when a more specific fact-bearing term is present. The question with the answer restored "
            "must reproduce the same course-note sentence as the explanation."
        ),
        "true_false": (
            f"The required correct_index is {tf_index}. Index 0 means True and index 1 means False. "
            "For a True item, state the supported fact accurately. For a False item, change exactly one "
            "detail of the focus fact while keeping the wording close to the source. In both cases, the "
            "explanation must state the exact supported course-note fact. Do not change the required index."
        ),
        "short_answer": (
            "Use the REQUIRED SHORT-ANSWER KEY POINTS below as the marking rubric. The question subject must name the "
            "same course-note section, concept, list, or labelled items as those required points; do not ask only for broad "
            "points about the STUDENT TOPIC. If there is more than one point, the question must explicitly ask for that "
            "number of points/aspects or name every labelled item. For two independent defining traits, "
            "a natural plural question about characteristics is also acceptable. The key_points array, reference answer and explanation "
            "must cover every required point completely and accurately; never return only the first bullet."
        ),
    }
    short_answer_points = [text for text, _chunk in _short_answer_evidence(focus, chunks, topic)] if quiz_type == "short_answer" else []
    required_short_answer = "\n".join(
        f"{index}. {point}" for index, point in enumerate(short_answer_points, 1)
    ) or "(not applicable)"
    return f"""TASK: Generate exactly one {quiz_type} exercise in English.
REQUIRED OUTPUT SCHEMA: {schemas[quiz_type]}
Use the references and allowed term pairs as the source of truth for the question, correct answer, explanation, and citations.
For MCQ only, the three incorrect distractors may be AI-generated and do not need to appear in the references, but they must be plausible, clearly incorrect, parallel in form, and must not create a second defensible correct answer.
The question must test the FOCUS FACT and remain relevant to STUDENT TOPIC.
FOCUS FACT: {focus.text}
STUDENT TOPIC: {topic or '(chapter-wide)'}
FACT KEYS ALREADY USED: {json.dumps([key for key in (history or {}).get('facts', []) if key != focus.fact_key][-20:])}
QUESTION FINGERPRINTS ALREADY USED: {json.dumps((history or {}).get('fingerprints', [])[-20:])}
QUALITY RULE FOR THIS TYPE: {type_rules[quiz_type]}
REQUIRED SHORT-ANSWER KEY POINTS:
{required_short_answer}
For MCQ, the correct option must be directly supported by the supplied references. Incorrect distractors may be synthesized by the AI when the notes do not provide enough alternatives.
For Fill Blank, answer must occur verbatim and contain at most three words.
Return only one JSON object with the required field names.

ALLOWED TERM PAIRS:
{allowed_terms or '(none)'}

REFERENCES:
{references}
"""


def generate_quiz(
    quiz_type: str,
    topic: str,
    search_result: dict[str, Any],
    terms: list[dict[str, Any]],
    history: dict,
    router: CozeAgentRouter,
    user_id: str,
    generation_seed: str = "",
    allow_agent: bool = True,
    local_source: str = "local_fallback",
) -> GeneratedQuiz:
    if quiz_type not in QUIZ_TYPES:
        raise QuizUnavailableError("Unsupported exercise type.")
    chunks = list(search_result.get("results") or [])
    attempt_index = len((history or {}).get("fingerprints", [])) if quiz_type == "true_false" else 0
    true_false_correct_index = _true_false_target(topic, generation_seed or user_id, attempt_index)
    if quiz_type == "true_false":
        try:
            focus = _true_false_focus(chunks, history, topic, true_false_correct_index)
        except QuizUnavailableError:
            if true_false_correct_index != 1:
                raise
            # The references may support a true proposition but no bounded
            # false variation. Do not reject sound evidence just to meet a
            # preferred answer balance.
            focus = _true_false_focus(chunks, history, topic, 0)
            true_false_correct_index = 0
    elif quiz_type == "short_answer":
        focus = _short_answer_focus(chunks, history, topic)
    else:
        focus = select_focus(chunks, history, topic)
    quiz = None
    citations: list[dict[str, Any]] = []
    source = local_source
    use_quiz_agent = bool(allow_agent and quiz_type in coze_quiz_types())
    if use_quiz_agent and router.config(AgentKind.QUIZ).configured:
        try:
            response = router.request(
                AgentKind.QUIZ,
                generation_prompt(
                    quiz_type,
                    topic,
                    focus,
                    chunks,
                    terms,
                    history,
                    true_false_correct_index=true_false_correct_index,
                ),
                user_id,
            )
            quiz = normalize_quiz(response.content, quiz_type, focus.fact_key)
            if quiz_type == "true_false" and int(quiz.get("correct_index", -1)) != true_false_correct_index:
                raise QuizValidationError("The True/False Agent ignored the required answer balance target.")
            if quiz_type == "short_answer":
                quiz, citations = _prepare_short_answer_quiz(quiz, focus, chunks, topic)
            else:
                citations = [citation(focus.chunk, focus.text)]
            validate_grounding(quiz, chunks, terms, focus)
            source = "coze_quiz_agent"
        except (CozeTransportError, QuizValidationError, ValueError):
            quiz = None
    if quiz is None:
        try:
            quiz, citations = local_quiz(
                quiz_type,
                focus,
                chunks,
                terms,
                topic,
                true_false_correct_index=true_false_correct_index,
            )
        except QuizUnavailableError:
            if quiz_type != "fill_blank":
                raise
            # The current unused fact may be valid for another question type
            # but contain no concise blankable term. Search the remaining
            # retrieved facts before reporting a learner-facing failure.
            focus = _alternate_fill_blank_focus(focus, chunks, terms, history, topic)
            quiz, citations = local_quiz(quiz_type, focus, chunks, terms, topic)
        validate_grounding(quiz, chunks, terms, focus)
    question_key = f"Q-{fingerprint(quiz.get('question'))}"
    return GeneratedQuiz(quiz, source, focus, citations, question_key, bool(search_result.get("coverage_exhausted")))

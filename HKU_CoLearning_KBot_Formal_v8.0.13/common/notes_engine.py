from __future__ import annotations

import re
from typing import Any

from knowledge_service.retrieval import lexical_score

from .coze_transport import AgentKind, CozeAgentRouter, CozeTransportError
from .json_recovery import coerce_bool, parse_json_like
from .quiz_parser import clean_text


NOT_FOUND = "I could not find this information in the uploaded course notes."


def references_from_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        output.append({
            "document_id": chunk.get("document_id", ""), "reference_id": index, "source_file": chunk.get("source_file", ""),
            "chapter": chunk.get("chapter", ""), "section": chunk.get("section", ""), "page": chunk.get("page"),
            "excerpt": clean_text(chunk.get("text"))[:850], "chunk_id": chunk.get("chunk_id", ""),
            "knowledge_key": chunk.get("knowledge_key", ""), "score": chunk.get("relevance", 0),
        })
    return output


def notes_prompt(question: str, references: list[dict[str, Any]]) -> str:
    context = "\n\n".join(
        f"[Reference {item['reference_id']}] File={item['source_file']} | Chapter={item['chapter']} | Section={item['section']} | Page={item['page']}\n{item['excerpt']}"
        for item in references
    )
    return f"""TASK: Answer one student's question. Lead with a useful direct answer. Assess completeness ONLY against what the question asks, not a broader syllabus. Do not require server hierarchies or resolution steps for a simple definition question. Always supply useful outside_course explanation when course notes lack needed information, unless genuinely uncertain.
Preserve the exact spelling and letter order of the student's term (DNF is not DFN or DNS). Never silently correct or expand an acronym. If an unfamiliar or ambiguous abbreviation lacks subject context, ask for the subject or surrounding sentence using evidence_status=clarify and no answer sections. Suggest a possible typo only as a question, never as an assumed correction. Do not list speculative expansions or claim a meaning is "most common" without evidence. If web results show multiple meanings, explain that ambiguity and ask the student to choose a domain. These ambiguity rules take priority over the instruction to provide a useful answer.
Use ALLOWED REFERENCES as the primary course context when they are relevant. You may use your general knowledge to interpret fragmentary or incomplete note text, connect implied facts, or answer when the retrieved references are insufficient.
For questions asking for conditions, assumptions, requirements, components, or factors, check ALL relevant references and include each distinct supported condition. Do not confuse a prerequisite with an advantage, disadvantage, consequence, or example. If the references give only a partial list, say which parts the notes support and identify any extra explanation as general knowledge.
For mathematics, preserve the meaning and direction of conditional probabilities. Never invent a formula or guess a symbol missing from an extracted slide. If equation text is corrupt or incomplete, explain only the relationship supported by readable words, and acknowledge a missing formula when necessary.
Do not invent filenames, pages, quotations, or reference IDs. Cite only reference IDs that genuinely contributed to the answer; if the answer comes only from general knowledge, use an empty used_reference_ids list.
Set insufficient_information=true only when you cannot provide a useful answer at all.
STUDENT QUESTION: {question}
ALLOWED REFERENCE IDS: {[item['reference_id'] for item in references]}

ALLOWED REFERENCES:
{context}

Separate course-supported claims from general knowledge. Each course section requires its supporting reference IDs. Put extra knowledge only in outside_course sections with no references.
Classify evidence_status as supported, partial, clarify, or unsupported. Assess only the supplied excerpts, not the entire course. supported requires complete course evidence; partial requires supported sections plus missing_information naming what is missing; clarify requires clarification_question and no answer sections; unsupported has no course sections and may include optional outside_course sections. General knowledge never counts as course support.
Return only this JSON schema:
{{"evidence_status":"partial","missing_information":"State the specific missing part","clarification_question":"","sections":[{{"kind":"course","text":"Supported explanation","reference_ids":[1]}},{{"kind":"outside_course","text":"Optional extra explanation","reference_ids":[],"web_reference_ids":[]}}],"insufficient_information":false}}
"""


def normalize_notes(raw: object, references: list[dict[str, Any]]) -> dict[str, Any]:
    payload = parse_json_like(raw)
    if not isinstance(payload, dict):
        raise ValueError("Notes output is not an object.")
    if "sections" in payload:
        allowed_ids = {r["reference_id"] for r in references}
        sections = []
        if not isinstance(payload["sections"], list):
            raise ValueError("Invalid sections")
        for section in payload["sections"]:
            if not isinstance(section, dict):
                raise ValueError("Invalid section")
            kind, text, ids = section.get("kind"), clean_text(section.get("text")), section.get("reference_ids", [])
            if kind not in {"course", "outside_course"} or not text or not isinstance(ids, list):
                raise ValueError("Invalid section")
            ids = list(dict.fromkeys(i for i in ids if type(i) is int and i in allowed_ids))
            if kind == "course" and not ids:
                raise ValueError("Course claim has no valid citation")
            sections.append({"kind": kind, "text": text, "reference_ids": ids if kind == "course" else [], "web_reference_ids": [i for i in section.get("web_reference_ids", []) if type(i) is int] if isinstance(section.get("web_reference_ids", []), list) else []})
        if not sections and payload.get("evidence_status") not in {"clarify", "unsupported"} and not coerce_bool(payload.get("insufficient_information"), False):
            raise ValueError("Empty sections")
        if coerce_bool(payload.get("insufficient_information"), False):
            sections = []
        used = list(dict.fromkeys(i for section in sections for i in section["reference_ids"]))
        answer = "\n\n".join(("Course notes: " if x["kind"] == "course" else "Outside course materials: ") + x["text"] for x in sections) or NOT_FOUND
        return {"answer": answer, "sections": sections, "used_reference_ids": used, "insufficient_information": not sections, "evidence_status": payload.get("evidence_status"), "missing_information": clean_text(payload.get("missing_information")), "clarification_question": clean_text(payload.get("clarification_question"))}
    allowed = {item["reference_id"] for item in references}
    used: list[int] = []
    value = payload.get("used_reference_ids", payload.get("citations", []))
    for item in value if isinstance(value, list) else []:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number in allowed and number not in used:
            used.append(number)
    insufficient = coerce_bool(payload.get("insufficient_information"), False)
    answer = clean_text(payload.get("answer"))
    if insufficient:
        answer, used = NOT_FOUND, []
    elif not answer:
        raise ValueError("Notes answer lacks a valid answer.")
    return {"answer": answer, "used_reference_ids": used, "insufficient_information": insufficient}


def local_notes_answer(question: str, references: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[tuple[float, str, int]] = []
    for reference in references:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reference["excerpt"]):
            sentence = clean_text(sentence).strip("•–- ")
            score = lexical_score(question, sentence)
            if len(sentence) >= 30 and score > 0:
                candidates.append((score, sentence, reference["reference_id"]))
    candidates.sort(reverse=True)
    if not candidates or candidates[0][0] < 0.18:
        return {"answer": NOT_FOUND, "used_reference_ids": [], "insufficient_information": True}
    chosen = candidates[:3]
    answer_parts: list[str] = []
    used: list[int] = []
    for _, sentence, reference_id in chosen:
        if sentence not in answer_parts:
            answer_parts.append(sentence)
        if reference_id not in used:
            used.append(reference_id)
    return {"answer": " ".join(answer_parts), "used_reference_ids": used, "insufficient_information": False}



def bare_acronym(question):
    match = re.fullmatch(r"\s*(?:(?:what\s+is|what\s+does|define|explain|meaning\s+of)\s+)?([A-Za-z][A-Za-z0-9-]{1,11})(?:\s+(?:mean|stand\s+for))?\s*[?.!]*\s*", question, re.I)
    if not match:
        return None
    term = match.group(1)
    # Upper-case abbreviations and known ambiguous lower-case spellings.
    # Do not classify every short English word (e.g. cat) as an acronym.
    return term if term.isupper() or term.casefold() in {"dnf", "dfn", "xyz"} else None


def clarify_term(term):
    result = apply_evidence_policy({"sections": [], "evidence_status": "clarify", "clarification_question": f'I could not establish the intended meaning of "{term}" from the retrieved notes. Which subject or sentence does it appear in? Please keep the original spelling or confirm a correction. You may also choose web sources to look up possible meanings.'})
    result.update(citations=[], answering_source="local_clarification", search_status="not_requested")
    return result


def answer_notes(question: str, chunks: list[dict[str, Any]], router: CozeAgentRouter, user_id: str) -> dict[str, Any]:
    if re.fullmatch(r"(?:why|how|explain|explain (?:it|this|that)|what (?:is|does) (?:it|this|that)(?: mean)?)\W*", question.strip(), re.I):
        result = apply_evidence_policy({"sections": [], "evidence_status": "clarify", "clarification_question": "Which concept or statement do you mean? Please name it or paste the relevant sentence."})
        result.update(citations=[], answering_source="local_clarification")
        return result
    term = bare_acronym(question)
    common_network_terms = {"DNS", "DNSSEC", "HTTP", "HTTPS", "TCP", "UDP", "IP", "IPV4", "IPV6", "URL", "HTML", "DHCP", "TLS", "SSL"}
    if term and term.upper() not in common_network_terms and not getattr(router, "web_search", False):
        if not any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", str(c.get("text", "")), re.I) for c in chunks):
            return clarify_term(term)
    references = references_from_chunks(chunks)
    result = None
    source = "local_fallback"
    if router.config(AgentKind.NOTES).configured:
        try:
            response = router.request(AgentKind.NOTES, notes_prompt(question, references), user_id)
            result = normalize_notes(response.content, references)
            if "sections" not in result:
                raise ValueError("Agent must separate course and outside-course explanations")
            if term:
                answer_text = " ".join(section["text"] for section in result["sections"])
                alternatives = re.findall(r"\b[A-Z][A-Z0-9-]{1,11}\b", answer_text)
                if not re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", answer_text, re.I) and any(t != term.upper() and sorted(t) == sorted(term.upper()) for t in alternatives):
                    return clarify_term(term)
            allowed_web = {item['id']: item for item in getattr(response, 'web_sources', [])}
            for section in result['sections']:
                section['web_sources'] = [allowed_web[i] for i in dict.fromkeys(section.get('web_reference_ids', [])) if i in allowed_web] if section['kind']=='outside_course' else []
            result['web_results'] = list(allowed_web.values())
            result['search_status'] = getattr(response, 'search_status', 'not_enabled')
            source = getattr(router, "source", "coze_notes_agent")
        except (CozeTransportError, ValueError):
            result = None
    if result is None:
        result = local_notes_answer(question, references)
    if "sections" not in result:
        result["sections"] = [] if result["insufficient_information"] else [{"kind": "course", "text": result["answer"], "reference_ids": result["used_reference_ids"]}]
    result = apply_evidence_policy(result)
    used = set(result["used_reference_ids"])
    result["citations"] = [item for item in references if item["reference_id"] in used]
    result["answering_source"] = source
    return result



def apply_evidence_policy(result):
    """Conservative compatibility policy; lexical overlap never proves completeness."""
    course = [s for s in result["sections"] if s["kind"] == "course"]
    outside = [s for s in result["sections"] if s["kind"] == "outside_course"]
    status = result.get("evidence_status")
    missing = result.get("missing_information") or ""
    clarification = result.get("clarification_question") or ""
    if status == "clarify" and clarification:
        course, outside = [], []
    else:
        if status not in {"supported", "partial", "unsupported"}:
            status = "partial" if course else "unsupported"
        if not course:
            status = "unsupported"
        elif status == "unsupported" or (status == "supported" and missing):
            status = "partial"
    if status == "partial" and not missing:
        missing = "Relevant excerpts were found, but complete coverage of your question could not be verified. Please specify any remaining part."
    messages = {"supported":"The retrieved course excerpts support this answer.", "partial":"The retrieved excerpts support only part of the answer.", "clarify":clarification, "unsupported":"No supporting evidence was found in the retrieved course excerpts. This does not prove it is absent from the whole course."}
    result.update(evidence_status=status, missing_information=missing if status=="partial" else "", clarification_question=clarification if status=="clarify" else "", evidence_message=messages[status], sections=course+outside, used_reference_ids=list(dict.fromkeys(i for s in course for i in s["reference_ids"])), insufficient_information=status in {"clarify","unsupported"})
    result["answer"] = "\n\n".join([messages[status]] + ([missing] if status=="partial" else []) + [("Course notes: " if s["kind"]=="course" else "Outside course materials: ")+s["text"] for s in course+outside])
    return result

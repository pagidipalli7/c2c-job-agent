"""FORBIDDEN question classes: never auto-answered by the LLM. Only a stored client-approved answer
(AnswerBank source=client, or a profile-derived fact) may answer these; otherwise they escalate.
NON-NEGOTIABLE product rule - do not weaken."""
from __future__ import annotations

import re

FORBIDDEN_PATTERNS: dict[str, re.Pattern] = {
    "salary": re.compile(r"\b(salary|compensation|comp\b|pay\s*(rate|range|expectation)|desired\s*(pay|rate|salary)|hourly rate|bill rate|expected (pay|rate)|base pay|otc|total comp|\$\s*\d|rate expectation)", re.I),
    "relocation": re.compile(r"\b(relocat\w*|willing to move|open to moving|move to)\b", re.I),
    "criminal": re.compile(r"\b(convict|felony|felon|misdemeanor|criminal|arrest|offen[cs]e|background check|crime)\b", re.I),
    "clearance": re.compile(r"\b(security clearance|clearance|secret|top secret|ts/sci|polygraph|public trust|dod\b)", re.I),
    "license": re.compile(r"\b(licen[cs]e|licensed|licensure|certified public|bar admission|driver'?s licen[cs]e|cdl\b|registered nurse|professional registration)\b", re.I),
    "references": re.compile(r"\b(reference|referee)s?\b", re.I),
    "start_date": re.compile(r"\b(start date|available to start|earliest (start|availability)|when (can|could) you (start|begin)|notice period|availability date|date available)\b", re.I),
    "legal": re.compile(r"\b(non-?compete|non-?solicit|lawsuit|litigation|legal (action|dispute|proceeding)|sued|bankrupt|garnish|export control|itar|ear\b|debar|sanction|terminated for cause|fired|drug (test|screen)|conflict of interest|contractual obligation|bound by|restrictive covenant)\b", re.I),
    "work_auth": re.compile(r"\b(authori[sz]ed to work|work authori[sz]ation|sponsorship|sponsor|visa|citizen|citizenship|green card|permanent resident|immigration|h-?1b|opt\b|cpt\b|ead\b|right to work)\b", re.I),
    "age_or_protected": re.compile(r"\b(date of birth|birth ?date|how old|your age|\bssn\b|social security|marital|pregnan|religion)\b", re.I),
}


def forbidden_class(question: str) -> str | None:
    """Return the forbidden class name for a question, or None if it may be auto-answered."""
    q = question or ""
    for name, pat in FORBIDDEN_PATTERNS.items():
        if pat.search(q):
            return name
    return None


def is_forbidden(question: str) -> bool:
    return forbidden_class(question) is not None

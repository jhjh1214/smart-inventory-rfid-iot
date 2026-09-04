"""Natural-language assistant over the inventory, pipeline, and audit data.

This is the "LLM Assistant" named in the UEC Figure 1 architecture caption. It
answers questions like "which items will run out first?" or "why did item-003
raise a security alert yesterday?" by letting a model call the read-only tools in
assistant_tools against the same functions the dashboard uses.

PROVIDER SELECTION
    ASSISTANT_PROVIDER picks the backend, default 'gemini':

        gemini     google-genai, free AI Studio tier   GEMINI_API_KEY
        anthropic  Claude, paid per token              ANTHROPIC_API_KEY

    Both adapters expose the same ask()/is_available() interface and share one
    tool layer and one system prompt, so switching provider changes no
    behaviour the dashboard can see beyond answer quality. Neither SDK is a hard
    dependency: with both absent the backend still boots and this endpoint
    reports itself unavailable.
"""
import os

import assistant_tools as tools
from assistant_tools import (MAX_HISTORY_TURNS, MAX_QUESTION_CHARS,  # noqa: F401
                             SYSTEM_PROMPT, TOOLS, validate_question)

import assistant_anthropic
import assistant_gemini

DEFAULT_PROVIDER = 'gemini'
_PROVIDERS = {
    assistant_gemini.NAME:    assistant_gemini,
    assistant_anthropic.NAME: assistant_anthropic,
}


class AssistantUnavailable(RuntimeError):
    """The assistant cannot run: SDK missing, no credentials, unknown provider."""


class AssistantError(RuntimeError):
    """The assistant ran but the provider call failed."""


class AssistantRateLimited(AssistantError):
    """The provider refused the call because a quota or rate limit was hit."""


# Matched against the provider's error text. Both SDKs raise their own
# exception types with the HTTP status in the message, and neither exposes a
# shared base class, so this stays a deliberate heuristic: a missed match
# degrades to a generic error, never to a wrong answer.
_RATE_LIMIT_MARKERS = ('429', 'resource_exhausted', 'rate limit',
                       'rate_limit', 'quota', 'too many requests')


def _is_rate_limit(message):
    low = (message or '').lower()
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def provider_name():
    return (os.environ.get('ASSISTANT_PROVIDER') or DEFAULT_PROVIDER).strip().lower()


def _provider():
    name = provider_name()
    mod = _PROVIDERS.get(name)
    if mod is None:
        raise AssistantUnavailable(
            "unknown ASSISTANT_PROVIDER '%s' - expected one of: %s"
            % (name, ', '.join(sorted(_PROVIDERS))))
    return mod


def is_available():
    try:
        return _provider().is_available()
    except AssistantUnavailable:
        return False


def unavailable_reason():
    try:
        return _provider().unavailable_reason()
    except AssistantUnavailable as e:
        return str(e)


def status():
    """Describe the assistant's readiness, for the dashboard and /api/assistant."""
    name = provider_name()
    available = is_available()
    return {
        'available': available,
        'provider': name,
        'reason': '' if available else unavailable_reason(),
        'tools': [fn.__name__ for fn in tools.TOOLS],
        'max_question_chars': tools.MAX_QUESTION_CHARS,
    }


def ask(question, history=None):
    """Answer `question` using the configured provider.

    Returns {'answer', 'tools_used', 'model', 'provider'}.
    Raises ValueError for bad input, AssistantUnavailable when not configured,
    AssistantError when the provider call fails.
    """
    question = validate_question(question)
    mod = _provider()
    if not mod.is_available():
        raise AssistantUnavailable(mod.unavailable_reason())
    try:
        return mod.ask(question, history)
    except Exception as e:
        if _is_rate_limit(str(e)):
            raise AssistantRateLimited(str(e))
        raise AssistantError(str(e))

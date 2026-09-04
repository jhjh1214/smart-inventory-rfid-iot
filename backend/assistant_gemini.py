"""Gemini adapter for the assistant.

Uses google-genai's automatic function calling: the plain typed functions in
assistant_tools are handed to the SDK, which introspects their signatures and
docstrings to build the schema, calls them when the model asks, and feeds the
results back without a hand-written loop.

Driven through client.chats, not client.models.generate_content. The SDK warns
that automatic function calling on generate_content is not the supported path,
and Chat also carries prior turns natively, so history needs no hand-assembly.

The SDK is an optional dependency, guarded like sklearn in analytics.py, so the
backend still boots and the LAN deployment still works when it is absent.
"""
import os

import assistant_tools as tools

try:
    from google import genai
    from google.genai import types
    _SDK = True
except ImportError:                                  # pragma: no cover - env dependent
    _SDK = False

NAME = 'gemini'

# Pinned rather than tracking gemini-flash-latest: a moving default would
# silently change answers between demo runs and invalidate anything the report
# quotes. gemini-2.5-flash was the previous default and now returns 404 for new
# API keys - the API itself points at 3.6. Override with GEMINI_MODEL.
DEFAULT_MODEL = 'gemini-3.6-flash'
MODEL = os.environ.get('GEMINI_MODEL', DEFAULT_MODEL)

# Cap on tool round-trips inside one answer, so a confused model cannot loop the
# database indefinitely on a free-tier quota.
MAX_TOOL_CALLS = 8

# The free tier returns a transient 503 ("high demand") often enough to spoil a
# live demo, so retry through the SDK's own backoff rather than surfacing it.
# Read-only tools make a retry harmless to repeat.
#
# 429 is deliberately NOT retried: the free tier's quota is per-minute, which a
# few seconds of backoff will not clear, so retrying only spends more of it.
# It is surfaced as a rate-limit error instead - see assistant.ask.
RETRY_STATUS   = [500, 502, 503, 504]
RETRY_ATTEMPTS = 3


def _api_key():
    return os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or ''


def is_available():
    return bool(_SDK and _api_key())


def unavailable_reason():
    if not _SDK:
        return 'the google-genai package is not installed (pip install google-genai)'
    if not _api_key():
        return 'neither GEMINI_API_KEY nor GOOGLE_API_KEY is set on the server'
    return ''


def _history(history):
    """Prior turns as Gemini Content. Gemini calls the assistant role 'model'."""
    return [
        types.Content(role=('model' if turn['role'] == 'assistant' else 'user'),
                      parts=[types.Part(text=turn['content'])])
        for turn in tools.clean_history(history)
    ]


def _calls_in(contents):
    names = []
    for content in (contents or []):
        for part in (getattr(content, 'parts', None) or []):
            call = getattr(part, 'function_call', None)
            if call is not None and getattr(call, 'name', None):
                names.append(call.name)
    return names


def _tool_names(chat, response):
    """Tool names actually invoked on this turn.

    On the Chat path the SDK records automatic function calls in the chat's
    own history; response.automatic_function_calling_history stays empty
    there. Seeded history carries text parts only, so every function_call
    part in the chat belongs to this turn. The response is kept as a
    fallback so the reader still works if that ever moves.
    """
    try:
        names = _calls_in(chat.get_history())
    except Exception:
        names = []
    if not names:
        names = _calls_in(getattr(response, 'automatic_function_calling_history', None))
    return names


def ask(question, history=None):
    """Answer `question`. Returns {'answer', 'tools_used', 'model', 'provider'}."""
    client = genai.Client(
        api_key=_api_key(),
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(
                attempts=RETRY_ATTEMPTS,
                initial_delay=1.0,
                max_delay=8.0,
                http_status_codes=RETRY_STATUS,
            ),
        ),
    )
    chat = client.chats.create(
        model=MODEL,
        history=_history(history),
        config=types.GenerateContentConfig(
            system_instruction=tools.SYSTEM_PROMPT,
            tools=tools.TOOLS,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_CALLS,
            ),
        ),
    )
    response = chat.send_message(question)

    answer = (getattr(response, 'text', None) or '').strip()
    if not answer:
        # A blocked or empty candidate still returns 200 with no text.
        feedback = getattr(response, 'prompt_feedback', None)
        blocked = getattr(feedback, 'block_reason', None) if feedback else None
        if blocked:
            raise RuntimeError('the model declined to answer (%s)' % blocked)
        answer = '(no answer produced)'

    return {
        'answer': answer,
        'tools_used': _tool_names(chat, response),
        'model': MODEL,
        'provider': NAME,
    }

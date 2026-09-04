"""Gemini adapter for the assistant.

Uses google-genai's automatic function calling: the plain typed functions in
assistant_tools are handed to the SDK, which introspects their signatures and
docstrings to build the schema, calls them when the model asks, and feeds the
results back without a hand-written loop.

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

NAME          = 'gemini'
DEFAULT_MODEL = 'gemini-2.5-flash'

# The free AI Studio tier covers the flash models. Override per deployment.
MODEL = os.environ.get('GEMINI_MODEL', DEFAULT_MODEL)

# Cap on tool round-trips inside one answer, so a confused model cannot loop the
# database indefinitely on a free-tier quota.
MAX_TOOL_CALLS = 8


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


def _contents(history, question):
    """Build the Gemini contents list. Gemini calls the assistant role 'model'."""
    contents = []
    for turn in tools.clean_history(history):
        role = 'model' if turn['role'] == 'assistant' else 'user'
        contents.append(types.Content(role=role,
                                      parts=[types.Part(text=turn['content'])]))
    contents.append(types.Content(role='user', parts=[types.Part(text=question)]))
    return contents


def _tool_names(response):
    """Tool names actually invoked, read off the automatic-calling history."""
    names = []
    for content in (getattr(response, 'automatic_function_calling_history', None) or []):
        for part in (getattr(content, 'parts', None) or []):
            call = getattr(part, 'function_call', None)
            if call is not None and getattr(call, 'name', None):
                names.append(call.name)
    return names


def ask(question, history=None):
    """Answer `question`. Returns {'answer', 'tools_used', 'model', 'provider'}."""
    client = genai.Client(api_key=_api_key())
    response = client.models.generate_content(
        model=MODEL,
        contents=_contents(history, question),
        config=types.GenerateContentConfig(
            system_instruction=tools.SYSTEM_PROMPT,
            tools=tools.TOOLS,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_CALLS,
            ),
        ),
    )

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
        'tools_used': _tool_names(response),
        'model': MODEL,
        'provider': NAME,
    }

"""Claude adapter for the assistant.

Kept as an alternative to the default Gemini provider: Claude is stronger at
multi-step tool use, at roughly $0.03-0.06 per question on Opus 5, so it is the
one to switch to if the free tier proves too weak for a demo. Select it with
ASSISTANT_PROVIDER=anthropic.

Uses the SDK's beta tool runner, which drives the request -> execute -> loop
cycle over the plain functions in assistant_tools. The SDK is an optional
dependency, guarded like sklearn in analytics.py.
"""
import os

import assistant_tools as tools

try:
    import anthropic
    from anthropic import beta_tool
    _SDK = True
except ImportError:                                  # pragma: no cover - env dependent
    _SDK = False

NAME          = 'anthropic'
DEFAULT_MODEL = 'claude-opus-5'

MODEL      = os.environ.get('ANTHROPIC_MODEL', DEFAULT_MODEL)
MAX_TOKENS = 16000

# Server-side refusal fallback, recommended for Opus 5. Set ASSISTANT_NO_FALLBACK=1
# if the beta is not enabled on the account: the adapter works without it, it
# just loses automatic re-routing when a request trips a safety classifier.
FALLBACK_BETA = 'server-side-fallback-2026-07-01'


def _wrapped_tools():
    """assistant_tools functions, decorated for the Anthropic tool runner."""
    return [beta_tool(fn) for fn in tools.TOOLS]


def is_available():
    return bool(_SDK and os.environ.get('ANTHROPIC_API_KEY'))


def unavailable_reason():
    if not _SDK:
        return 'the anthropic package is not installed (pip install anthropic)'
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return 'ANTHROPIC_API_KEY is not set on the server'
    return ''


def ask(question, history=None):
    """Answer `question`. Returns {'answer', 'tools_used', 'model', 'provider'}."""
    client = anthropic.Anthropic()
    messages = tools.clean_history(history) + [{'role': 'user', 'content': question}]

    kwargs = {
        'model': MODEL,
        'max_tokens': MAX_TOKENS,
        'system': tools.SYSTEM_PROMPT,
        'tools': _wrapped_tools(),
        'messages': messages,
        'thinking': {'type': 'adaptive'},
    }
    if not os.environ.get('ASSISTANT_NO_FALLBACK'):
        kwargs['betas'] = [FALLBACK_BETA]
        kwargs['fallbacks'] = 'default'

    tools_used, final = [], None
    for message in client.beta.messages.tool_runner(**kwargs):
        final = message
        for block in message.content:
            if getattr(block, 'type', None) == 'tool_use':
                tools_used.append(block.name)

    if final is None:
        raise RuntimeError('no response from the model')

    if getattr(final, 'stop_reason', None) == 'refusal':
        detail = getattr(final, 'stop_details', None)
        category = getattr(detail, 'category', None) if detail else None
        raise RuntimeError('the model declined to answer%s'
                           % (' (%s)' % category if category else ''))

    answer = '\n'.join(b.text for b in final.content
                       if getattr(b, 'type', None) == 'text' and getattr(b, 'text', ''))

    return {
        'answer': answer.strip() or '(no answer produced)',
        'tools_used': tools_used,
        'model': MODEL,
        'provider': NAME,
    }

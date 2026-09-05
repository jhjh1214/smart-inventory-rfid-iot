"""Per-item stock profiles: how the pipeline should treat a class of goods.

WHAT A PROFILE IS NOT
    It does not add pipeline stages. The five-stage state machine is unchanged,
    which matters because the UEC abstract's Figure 1 and the published state
    diagram describe it. A profile changes two decisions the warehouse gate
    makes when goods leave, nothing else:

      dispatch_terminal   False sends the tag to return_pending instead of the
                          terminal dispatched, so a returnable asset closes its
                          own loop on the next rack scan instead of waiting for
                          an admin to mark it for return by hand.

      require_supervisor  True refuses the dispatch outright when no supervisor
                          session is active, instead of allowing it and raising
                          an UNVERIFIED DISPATCH alert after the fact.

WHY BLOCKING IS PER PROFILE
    CLAUDE.md section 5 records that dispatch without a supervisor is detective
    rather than preventive on purpose: warehouse operations must never deadlock
    on a badge reader. That reasoning holds for ordinary stock, and stops
    holding for a single high-value serialised asset. Making the block opt-in
    per profile keeps both behaviours available without choosing globally.

DEFAULT
    'consumable' reproduces the original behaviour exactly. The migration
    defaults every existing row to it, so adding profiles changes nothing until
    an item is deliberately reclassified.
"""

DEFAULT = 'consumable'

PROFILES = {
    'consumable': {
        'label':              'Consumable',
        'description':        'Used up when issued. Dispatch is final.',
        'dispatch_terminal':  True,
        'require_supervisor': False,
    },
    'returnable': {
        'label':              'Returnable',
        'description':        'Tools and equipment expected back. Dispatch opens a return.',
        'dispatch_terminal':  False,
        'require_supervisor': False,
    },
    'serialised': {
        'label':              'Serialised',
        'description':        'High-value and individually tracked. Dispatch needs a supervisor.',
        'dispatch_terminal':  True,
        'require_supervisor': True,
    },
}

NAMES = tuple(PROFILES)


def is_valid(name):
    """True when `name` is a known profile."""
    return isinstance(name, str) and name.strip().lower() in PROFILES


def normalise(name):
    """A known profile name, falling back to DEFAULT for anything unrecognised.

    Used on the read path so a row written by an older build, or by hand, can
    never crash the pipeline - it just behaves as ordinary consumable stock.
    """
    return name.strip().lower() if is_valid(name) else DEFAULT


def policy(name):
    """The rule dict for `name`, defaulting for anything unrecognised."""
    return PROFILES[normalise(name)]


def describe():
    """Profile catalogue for the API and dashboard."""
    return [
        {'name': key, **{k: v for k, v in rule.items()}}
        for key, rule in PROFILES.items()
    ]

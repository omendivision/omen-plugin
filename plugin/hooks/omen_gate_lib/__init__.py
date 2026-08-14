"""Vendored copy of omen_admin_api.gate.{triggers,source}.

The hook runs on the engineer's machine with no access to our package, and
the trigger has to classify locally — asking a server on every shell command
would make the free path cost a network round trip. So these files are
copied verbatim at release, and tests/unit/test_plugin_parity.py asserts
they are byte-identical to the originals, because a lexicon that drifts
between the two is a gate that tests green and behaves differently.
"""

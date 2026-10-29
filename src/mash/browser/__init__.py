"""Per-user browsers: a Chromium the agents acting for one user share.

A browser holds one user's Chromium profile, with the sessions the user signed
in to, and nothing else. Its daemon (``daemon.py``) drives Chromium through
Playwright MCP and answers only the host, with the user's browser token
(``tokens.py``). Code in the user's sandbox has no route to it.
"""

"""Per-policy browsers: a Chromium the agents under one policy share.

A browser holds one policy's Chromium profile, with the sessions people signed
in to, and nothing else. Its daemon (``daemon.py``) drives Chromium through
Playwright MCP and answers only the host, with the policy's browser token
(``tokens.py``). Code in the policy's sandbox has no route to it.
"""

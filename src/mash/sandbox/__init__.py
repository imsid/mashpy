"""Per-user sandboxes: where the bash commands an agent's model writes run.

A sandbox holds one user's workspaces, ``/home/agent/workspaces/<agent_id>``,
and nothing else: no secrets, no database access, no route to the host. The
host reaches its daemon (``daemon.py``) over HTTP with the user's sandbox token
(``tokens.py``).
"""

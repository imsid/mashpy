"""Per-user sandboxes: where the code an agent's model writes runs.

A sandbox holds one user's workspaces, ``/home/agent/workspaces/<agent_id>``,
and nothing else: no secrets, no database access, no route to the host. The
host reaches its daemon (``daemon.py``) over HTTP with the user's sandbox token
(``tokens.py``).
"""

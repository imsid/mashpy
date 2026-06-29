"""Pooled agents: the Pilot primary and its five module copilots.

Each agent is a package with a `spec.py` implementation; this package's
submodules re-export the agent id, spec factory, and metadata builder that
`pilot.catalog` registers.
"""

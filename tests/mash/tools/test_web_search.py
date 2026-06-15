"""Tests for web search providers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mash.tools.web_search import ParallelSearchProvider


class ParallelSearchProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate from any ambient Parallel credentials.
        self._env = patch.dict(
            os.environ,
            {"PARALLEL_API_KEY": "", "PARALLEL_OAUTH_TOKEN": ""},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_anonymous_uses_free_endpoint_without_auth(self) -> None:
        config = ParallelSearchProvider().mcp_server_config()
        self.assertEqual(config.url, ParallelSearchProvider.FREE_URL)
        self.assertEqual(config.headers, {})
        self.assertEqual(config.allowed_tools, ["web_search", "web_fetch"])
        self.assertEqual(config.name, "parallel_web_search")

    def test_api_key_arg_uses_oauth_endpoint_with_bearer(self) -> None:
        config = ParallelSearchProvider(api_key="k").mcp_server_config()
        self.assertEqual(config.url, ParallelSearchProvider.OAUTH_URL)
        self.assertEqual(config.headers, {"Authorization": "Bearer k"})

    def test_oauth_token_takes_precedence_over_api_key(self) -> None:
        config = ParallelSearchProvider(
            api_key="k", oauth_token="t"
        ).mcp_server_config()
        self.assertEqual(config.headers, {"Authorization": "Bearer t"})

    def test_env_vars_are_picked_up(self) -> None:
        with patch.dict(os.environ, {"PARALLEL_API_KEY": "envkey"}):
            config = ParallelSearchProvider().mcp_server_config()
        self.assertEqual(config.headers, {"Authorization": "Bearer envkey"})

    def test_explicit_arg_beats_env_var(self) -> None:
        with patch.dict(os.environ, {"PARALLEL_API_KEY": "envkey"}):
            config = ParallelSearchProvider(api_key="argkey").mcp_server_config()
        self.assertEqual(config.headers, {"Authorization": "Bearer argkey"})

    def test_oauth_env_var_beats_api_key_env_var(self) -> None:
        with patch.dict(
            os.environ,
            {"PARALLEL_API_KEY": "envkey", "PARALLEL_OAUTH_TOKEN": "envtok"},
        ):
            config = ParallelSearchProvider().mcp_server_config()
        self.assertEqual(config.headers, {"Authorization": "Bearer envtok"})

    def test_base_url_override_is_respected(self) -> None:
        config = ParallelSearchProvider(
            base_url="https://example.test/mcp"
        ).mcp_server_config()
        self.assertEqual(config.url, "https://example.test/mcp")

    def test_allowed_tools_override_is_respected(self) -> None:
        config = ParallelSearchProvider(
            allowed_tools=["web_search"]
        ).mcp_server_config()
        self.assertEqual(config.allowed_tools, ["web_search"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for runtime tools."""

from __future__ import annotations

import json
import unittest

from mash.memory.search.types import SearchResult
from mash.tools.runtime import RuntimeToolBuilder


class FakeEventLogger:
    async def emit(self, _event: object) -> None:
        return None


class FakeStore:
    def __init__(self) -> None:
        self.turn_lookup_calls: list[list[dict[str, str]]] = []
        self.turn_lookup_result: list[dict[str, object]] | None = None
        self.turn_lookup_error: Exception | None = None

    async def get_turn_by_ids(
        self,
        pairs: list[dict[str, str]],
    ) -> list[dict[str, object]] | None:
        self.turn_lookup_calls.append(list(pairs))
        if self.turn_lookup_error is not None:
            raise self.turn_lookup_error
        return self.turn_lookup_result


class FakeSearchService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.results: list[SearchResult] = []
        self.results_by_query: dict[str, list[SearchResult]] = {}
        self.error: Exception | None = None

    async def search(
        self,
        query: str,
        *,
        app_id: str,
        limit: int = 10,
        session_id: str | None = None,
    ) -> list[SearchResult]:
        self.calls.append(
            {
                "query": query,
                "app_id": app_id,
                "limit": limit,
                "session_id": session_id,
            }
        )
        if self.error is not None:
            raise self.error
        if query in self.results_by_query:
            return self.results_by_query[query]
        return self.results


class RuntimeSearchToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.builder = RuntimeToolBuilder(
            store=self.store,  # type: ignore[arg-type]
            app_id="db",
            session_id="s1",
            event_logger=FakeEventLogger(),  # type: ignore[arg-type]
        )
        self.search_service = FakeSearchService()
        self.builder._search_service = self.search_service  # type: ignore[assignment]

        self.search_tool = next(
            tool
            for tool in self.builder.build_tools()
            if tool.name == "search_conversations"
        )

    async def test_search_session_scope(self) -> None:
        self.search_service.results = [
            SearchResult(
                turn_id="t1",
                session_id="s1",
                similarity_score=0.87,
                preview="hello preview",
            )
        ]

        result = await self.search_tool.execute(
            {"query": "@user:hello", "scope": "session", "limit": 5}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.search_service.calls[0]["session_id"], "s1")
        self.assertEqual(self.search_service.calls[0]["limit"], 5)
        payload = json.loads(result.content)
        self.assertEqual(payload["scope"], "session")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["turn_id"], "t1")

    async def test_search_app_scope_uses_none_session(self) -> None:
        result = await self.search_tool.execute(
            {"query": "@agent:answer", "scope": "app"}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.search_service.calls[0]["session_id"], None)
        payload = json.loads(result.content)
        self.assertEqual(payload["scope"], "app")
        self.assertIsNone(payload["session_id"])

    async def test_default_scope_is_session(self) -> None:
        result = await self.search_tool.execute({"query": "@user:test"})

        self.assertFalse(result.is_error)
        self.assertEqual(self.search_service.calls[0]["session_id"], "s1")
        self.assertEqual(self.search_service.calls[0]["limit"], 10)
        payload = json.loads(result.content)
        self.assertEqual(payload["scope"], "session")
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["effective_queries"], ["@user:test"])

    async def test_unprefixed_query_searches_both_roles_and_merges(self) -> None:
        self.search_service.results_by_query = {
            "@user:ad conversion metrics": [
                SearchResult(
                    turn_id="t1",
                    session_id="s1",
                    similarity_score=0.61,
                    preview="user preview",
                ),
                SearchResult(
                    turn_id="t2",
                    session_id="s2",
                    similarity_score=0.72,
                    preview="shared lower",
                ),
            ],
            "@agent:ad conversion metrics": [
                SearchResult(
                    turn_id="t2",
                    session_id="s2",
                    similarity_score=0.91,
                    preview="shared higher",
                ),
                SearchResult(
                    turn_id="t3",
                    session_id="s9",
                    similarity_score=0.55,
                    preview="agent preview",
                ),
            ],
        }

        result = await self.search_tool.execute(
            {"query": "ad conversion metrics", "scope": "app", "limit": 10}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(
            [call["query"] for call in self.search_service.calls],
            ["@user:ad conversion metrics", "@agent:ad conversion metrics"],
        )
        self.assertTrue(
            all(call["session_id"] is None for call in self.search_service.calls)
        )

        payload = json.loads(result.content)
        self.assertEqual(
            payload["effective_queries"],
            ["@user:ad conversion metrics", "@agent:ad conversion metrics"],
        )
        self.assertEqual(
            [item["turn_id"] for item in payload["results"]],
            ["t2", "t1", "t3"],
        )
        self.assertEqual(payload["results"][0]["similarity_score"], 0.91)
        self.assertEqual(payload["results"][0]["preview"], "shared higher")

    async def test_missing_or_blank_query_errors(self) -> None:
        missing = await self.search_tool.execute({})
        blank = await self.search_tool.execute({"query": "   "})

        self.assertTrue(missing.is_error)
        self.assertTrue(blank.is_error)
        self.assertEqual(self.search_service.calls, [])

    async def test_invalid_scope_errors(self) -> None:
        result = await self.search_tool.execute(
            {"query": "@user:test", "scope": "global"}
        )

        self.assertTrue(result.is_error)
        self.assertEqual(self.search_service.calls, [])

    async def test_invalid_limit_errors(self) -> None:
        result = await self.search_tool.execute({"query": "@user:test", "limit": "abc"})

        self.assertTrue(result.is_error)
        self.assertEqual(self.search_service.calls, [])

    async def test_empty_results_payload(self) -> None:
        self.search_service.results = []

        result = await self.search_tool.execute({"query": "@user:none"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["results"], [])

    async def test_service_exception_mapping(self) -> None:
        self.search_service.error = RuntimeError("boom")

        result = await self.search_tool.execute({"query": "@user:test"})

        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "boom")


class RuntimeFullTurnMessageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.builder = RuntimeToolBuilder(
            store=self.store,  # type: ignore[arg-type]
            app_id="db",
            session_id="s1",
            event_logger=FakeEventLogger(),  # type: ignore[arg-type]
        )
        self.builder._search_service = FakeSearchService()  # type: ignore[assignment]
        self.turn_tool = next(
            tool
            for tool in self.builder.build_tools()
            if tool.name == "get_full_turn_message"
        )

    async def test_successful_turn_fetch_returns_full_messages(self) -> None:
        self.store.turn_lookup_result = [
            {
                "turn_id": "t1",
                "session_id": "s2",
                "user_message": "hello",
                "agent_response": "world",
            },
            {
                "turn_id": "t3",
                "session_id": "s9",
                "user_message": "another",
                "agent_response": "reply",
            },
        ]

        result = await self.turn_tool.execute(
            {
                "pairs": [
                    {"turn_id": "t1", "session_id": "s2"},
                    {"turn_id": "t3", "session_id": "s9"},
                ]
            }
        )

        self.assertFalse(result.is_error)
        self.assertEqual(
            self.store.turn_lookup_calls,
            [[
                {"turn_id": "t1", "session_id": "s2"},
                {"turn_id": "t3", "session_id": "s9"},
            ]],
        )
        payload = json.loads(result.content)
        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["found_count"], 2)
        self.assertEqual(len(payload["turns"]), 2)
        self.assertEqual(payload["turns"][0]["user_message"], "hello")
        self.assertEqual(payload["turns"][1]["agent_response"], "reply")
        self.assertEqual(payload["missing_pairs"], [])

    async def test_missing_turns_return_empty_results_with_missing_pairs(self) -> None:
        self.store.turn_lookup_result = None

        result = await self.turn_tool.execute(
            {"pairs": [{"turn_id": "missing", "session_id": "s2"}]}
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["requested_count"], 1)
        self.assertEqual(payload["found_count"], 0)
        self.assertEqual(payload["turns"], [])
        self.assertEqual(
            payload["missing_pairs"],
            [{"turn_id": "missing", "session_id": "s2"}],
        )

    async def test_missing_pairs_errors(self) -> None:
        result = await self.turn_tool.execute({})

        self.assertTrue(result.is_error)
        self.assertEqual(self.store.turn_lookup_calls, [])

    async def test_pairs_must_be_non_empty_array(self) -> None:
        result = await self.turn_tool.execute({"pairs": []})

        self.assertTrue(result.is_error)
        self.assertEqual(self.store.turn_lookup_calls, [])

    async def test_invalid_pair_object_errors(self) -> None:
        result = await self.turn_tool.execute({"pairs": ["not-an-object"]})

        self.assertTrue(result.is_error)
        self.assertEqual(self.store.turn_lookup_calls, [])

    async def test_blank_ids_error(self) -> None:
        result_a = await self.turn_tool.execute(
            {"pairs": [{"turn_id": "   ", "session_id": "s2"}]}
        )
        result_b = await self.turn_tool.execute(
            {"pairs": [{"turn_id": "t1", "session_id": "   "}]}
        )

        self.assertTrue(result_a.is_error)
        self.assertTrue(result_b.is_error)
        self.assertEqual(self.store.turn_lookup_calls, [])

    async def test_store_exception_maps_to_tool_error(self) -> None:
        self.store.turn_lookup_error = RuntimeError("db failed")

        result = await self.turn_tool.execute(
            {"pairs": [{"turn_id": "t1", "session_id": "s2"}]}
        )

        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "failed to fetch turns: db failed")

    async def test_partial_matches_include_missing_pairs(self) -> None:
        self.store.turn_lookup_result = [
            {
                "turn_id": "t1",
                "session_id": "s2",
                "user_message": "hello",
                "agent_response": "world",
            }
        ]

        result = await self.turn_tool.execute(
            {
                "pairs": [
                    {"turn_id": "t1", "session_id": "s2"},
                    {"turn_id": "missing", "session_id": "s2"},
                ]
            }
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["found_count"], 1)
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(
            payload["missing_pairs"],
            [{"turn_id": "missing", "session_id": "s2"}],
        )


class RuntimeToolMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = RuntimeToolBuilder(
            store=FakeStore(),  # type: ignore[arg-type]
            app_id="db",
            session_id="s1",
            event_logger=FakeEventLogger(),  # type: ignore[arg-type]
        )
        self.builder._search_service = FakeSearchService()  # type: ignore[assignment]

    def test_default_runtime_tool_order_is_unchanged(self) -> None:
        tools = self.builder.build_tools()

        self.assertEqual(
            [tool.name for tool in tools],
            [
                "search_conversations",
                "get_full_turn_message",
                "get_user_preferences",
                "set_user_preferences",
                "list_app_data",
                "set_app_data",
            ],
        )

    def test_rewritten_descriptions_and_parameters_clarify_scope_and_usage(self) -> None:
        tools = {tool.name: tool for tool in self.builder.build_tools()}

        self.assertIn("current session", tools["get_user_preferences"].description)
        self.assertIn("current session", tools["set_user_preferences"].description)
        self.assertIn("current session", tools["list_app_data"].description)
        self.assertIn("session-scoped", tools["set_app_data"].description)

        search_tool = tools["search_conversations"]
        self.assertIn("ranked previews", search_tool.description)
        self.assertIn("session_id and turn_id", search_tool.description)
        self.assertEqual(search_tool.parameters["properties"]["query"]["type"], "string")
        self.assertEqual(
            search_tool.parameters["properties"]["scope"]["enum"],
            ["session", "app"],
        )
        self.assertEqual(search_tool.parameters["required"], ["query"])

        turn_tool = tools["get_full_turn_message"]
        self.assertIn("Expand", turn_tool.description)
        self.assertIn("full turn text", turn_tool.description)
        self.assertEqual(turn_tool.parameters["required"], ["pairs"])
        self.assertEqual(turn_tool.parameters["properties"]["pairs"]["type"], "array")

        set_preferences_tool = tools["set_user_preferences"]
        self.assertEqual(
            set_preferences_tool.parameters["properties"]["preferences"]["type"],
            "object",
        )

        set_app_data_tool = tools["set_app_data"]
        self.assertEqual(set_app_data_tool.parameters["properties"]["key"]["type"], "string")
        self.assertIn("value", set_app_data_tool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()

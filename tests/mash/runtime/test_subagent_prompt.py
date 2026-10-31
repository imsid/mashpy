"""The RunSubAgents directory in a host primary's system prompt."""

from __future__ import annotations

import unittest

from mash.runtime.host.subagents import AgentMetadata, build_subagent_prompt_block


def _meta(name: str) -> AgentMetadata:
    return AgentMetadata(
        display_name=name,
        description=f"{name} description.",
        capabilities=["conversation"],
        usage_guidance=f"Use {name}.",
    )


class SubagentDirectoryTests(unittest.TestCase):
    def test_the_agent_itself_is_listed_first(self) -> None:
        block = build_subagent_prompt_block(
            "base",
            {"writer": _meta("Writer"), "research": _meta("Research")},
            own_agent_id="concierge",
            own_metadata=_meta("Concierge"),
        )
        entries = [line for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual(
            entries,
            [
                "- concierge (you) | Concierge: Concierge description.",
                "- research | Research: Research description.",
                "- writer | Writer: Writer description.",
            ],
        )
        self.assertIn("Guidance: Use Concierge.", block)

    def test_it_says_when_to_split(self) -> None:
        block = build_subagent_prompt_block(
            "base", {"research": _meta("Research")}, own_agent_id="concierge"
        )
        self.assertIn("parts that do not depend on each other", block)
        self.assertIn("Answer directly when the task is small", block)
        # Without metadata the agent is still listed, by id.
        self.assertIn("\n- concierge (you)\n", block)

    def test_no_subagents_means_no_block(self) -> None:
        self.assertEqual(
            build_subagent_prompt_block("base", {}, own_agent_id="concierge"), "base"
        )

    def test_a_list_prompt_gets_a_text_block(self) -> None:
        block = build_subagent_prompt_block(
            [{"type": "text", "text": "base"}],
            {"research": _meta("Research")},
            own_agent_id="concierge",
        )
        self.assertEqual(len(block), 2)
        self.assertIn("concierge (you)", block[1]["text"])


if __name__ == "__main__":
    unittest.main()

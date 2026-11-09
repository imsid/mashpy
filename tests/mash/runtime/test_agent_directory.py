"""The AGENTS directory of a host's agents in the companion's system prompt."""

from __future__ import annotations

import unittest

from mash.runtime.host.directory import AgentMetadata, build_agent_directory


def _meta(name: str) -> AgentMetadata:
    return AgentMetadata(
        display_name=name,
        description=f"{name} description.",
        capabilities=["conversation"],
        usage_guidance=f"Use {name}.",
    )


class AgentDirectoryTests(unittest.TestCase):
    def test_lists_the_hosts_agents_by_id(self) -> None:
        block = build_agent_directory(
            "base", {"writer": _meta("Writer"), "research": _meta("Research")}
        )
        entries = [line for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual(
            entries,
            [
                "- research | Research: Research description.",
                "- writer | Writer: Writer description.",
            ],
        )
        self.assertIn("Guidance: Use Writer.", block)
        self.assertTrue(block.startswith("base\n\nAGENTS\n"))

    def test_it_says_when_to_split(self) -> None:
        block = build_agent_directory("base", {"research": None})
        self.assertIn("only when its parts do not depend on each other", block)
        self.assertIn("session_id?", block)
        # Without metadata the agent is still listed, by id.
        self.assertTrue(block.endswith("\n- research"))

    def test_no_agents_means_no_block(self) -> None:
        self.assertEqual(build_agent_directory("base", {}), "base")

    def test_a_list_prompt_gets_a_text_block(self) -> None:
        block = build_agent_directory(
            [{"type": "text", "text": "base"}], {"research": _meta("Research")}
        )
        self.assertEqual(len(block), 2)
        self.assertIn("- research | Research", block[1]["text"])


if __name__ == "__main__":
    unittest.main()

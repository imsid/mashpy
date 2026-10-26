"""An agent's sandbox variables: checked at startup and described to the model."""

from __future__ import annotations

import unittest

from mash.tools.sandbox import SandboxVariable, build_sandbox_tools

DB = SandboxVariable("postgresql://reader:pw@db/app", "A read-only Postgres URL.")


def _run_code(env):
    [tool] = build_sandbox_tools(
        host_api_url="http://host", token="tok", agent_id="research", env=env
    )
    return tool


class SandboxEnvTests(unittest.TestCase):
    def test_without_variables_the_sandbox_holds_no_secrets(self) -> None:
        self.assertIn("holds no secrets", _run_code(None).description)

    def test_the_model_reads_each_variables_description_not_its_value(self) -> None:
        tool = _run_code({"MASH_APP_DB_URL": DB})

        self.assertIn("- MASH_APP_DB_URL: A read-only Postgres URL.", tool.description)
        self.assertNotIn("reader:pw", tool.description)
        self.assertNotIn("holds no secrets", tool.description)

    def test_names_the_sandbox_refuses_fail_at_startup(self) -> None:
        for name in ("DATABASE_URL", "MASH_lower", "MASH_FILES_URL", "MASH_PORTS_URL"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _run_code({name: DB})

    def test_a_plain_string_is_not_a_variable(self) -> None:
        with self.assertRaises(ValueError):
            _run_code({"MASH_APP_DB_URL": "postgresql://reader:pw@db/app"})


if __name__ == "__main__":
    unittest.main()

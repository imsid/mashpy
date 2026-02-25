"""Data Agent CLI for BigQuery MCP exploration."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

import google.auth
from google.auth.transport.requests import Request

from mash.cli.app import AbstractMashApp
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider
from mash.mcp import MCPClientError, MCPServerConfig
from mash.memory.store import MemoryStore, SQLiteStore
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    BIGQUERY_ALLOWED_TOOLS,
    BIGQUERY_MCP_URL,
    BIGQUERY_PROJECT_ID,
)
from .local_tools import build_analyst_tools, build_steward_tools
from .prompt import (
    build_base_prompt,
    build_roles_context,
    build_schema_context,
)

APP_ID: str = "data-agent"
BIGQUERY_CONNECTION_NAME = "bigquery"


class DataAgentApp(AbstractMashApp):
    """CLI app for role-based BigQuery assistance."""

    def get_app_id(self) -> str:
        return APP_ID

    @staticmethod
    def _workspace_root() -> Path:
        return Path(__file__).resolve().parents[3]

    def get_log_destination(self) -> Path:
        return Path(__file__).resolve().parent / "logs" / "db.jsonl"

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            api_key=ANTHROPIC_API_KEY,
            app_id=APP_ID,
        )

    @staticmethod
    def get_cached_files() -> List[str]:
        db_dir = Path(__file__).resolve().parent
        candidates = [
            db_dir / "metrics_layer" / "schema" / "source.schema.yml",
            db_dir / "metrics_layer" / "schema" / "metric.schema.yml",
        ]
        return [str(path) for path in candidates if path.exists()]

    def get_system_prompt(self) -> List[Dict[str, Any]]:
        prompt: List[Dict[str, Any]] = [
            {
                "type": "text",
                "text": build_base_prompt(BIGQUERY_PROJECT_ID),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": build_roles_context(self.skills),
                "cache_control": {"type": "ephemeral"},
            },
        ]

        schema_context = build_schema_context(DataAgentApp.get_cached_files())
        if schema_context:
            prompt.append(
                {
                    "type": "text",
                    "text": schema_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return prompt

    def build_store(self) -> MemoryStore:
        db_path = Path(__file__).resolve().parent / ".mash" / "data-agent.db"
        return SQLiteStore(str(db_path))

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        local_tools = build_steward_tools(
            workspace_root=self._workspace_root()
        ) + build_analyst_tools(workspace_root=self._workspace_root())
        for tool in local_tools:
            tools.register(tool)
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skills_dir = Path(__file__).resolve().parent / ".mash" / "skills"
        for skill in skills.get_custom_skills(skills_dir):
            skills.register(skill)
        return skills

    def build_agent_config(self) -> AgentConfig:
        config = AgentConfig(
            app_id=self.get_app_id(),
            system_prompt=self.get_system_prompt(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            conversation_history_turns=3,
            compaction_token_threshold=100000,
            skills_enabled=True,
            tool_search_enabled=False,
        )
        return config

    def build_mcp_servers(self) -> List[MCPServerConfig]:
        try:
            access_token = self._generate_access_token()
        except RuntimeError as exc:
            self._startup_auth_error = str(exc)
            print(
                f"Warning: BigQuery MCP auth token could not be generated: {exc}",
                file=sys.stderr,
            )
            return []

        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        if BIGQUERY_PROJECT_ID:
            headers["x-goog-user-project"] = BIGQUERY_PROJECT_ID

        return [
            MCPServerConfig(
                name=BIGQUERY_CONNECTION_NAME,
                url=BIGQUERY_MCP_URL,
                description="BigQuery MCP server for data exploration",
                headers=headers,
                allowed_tools=BIGQUERY_ALLOWED_TOOLS,
            )
        ]

    @staticmethod
    def _generate_access_token() -> str:
        """Generate an access token via ADC credentials using google-auth."""
        try:
            credentials, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/bigquery"]
            )
            credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError(
                "Failed to generate BigQuery access token via ADC/google-auth"
                f": {exc}"
            ) from exc

        token = credentials.token
        if not token:
            raise RuntimeError("google-auth returned an empty access token")
        return token


def main() -> int:
    """Entry point for DataAgentApp."""
    app = None
    try:
        app = DataAgentApp()
        app.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except MCPClientError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if app:
            app.cleanup()


if __name__ == "__main__":
    sys.exit(main())

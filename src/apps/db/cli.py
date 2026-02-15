"""Data Agent CLI for BigQuery MCP exploration."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import google.auth
from google.auth.transport.requests import Request

from mash.cli.app import MashApp
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.mcp import MCPClientError
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
from .local_tools import build_local_tools
from .prompts import build_base_prompt, build_roles_context

APP_ID: str = "data-agent"
BIGQUERY_CONNECTION_NAME = "bigquery"


class DataAgentApp(MashApp):
    """CLI app for role-based BigQuery assistance."""

    def __init__(self) -> None:
        self.app_id: str = APP_ID
        self.workspace_root = Path(__file__).resolve().parents[3]
        self.store = self.register_memory_store()
        self.tools = self.register_tools()
        self.skills = self.register_skills()
        self.cached_files = self.register_cached_files()
        self.agent = self.register_agent()
        self._startup_auth_error: Optional[str] = None
        mcp_servers = self._build_mcp_servers()

        super().__init__(
            app_name=APP_ID,
            agent=self.agent,
            store=self.store,
            cached_files=self.cached_files,
            mcp_servers=mcp_servers,
            log_destination=DataAgentApp.get_logger_destination(),
        )

    @staticmethod
    def get_logger_destination() -> Path:
        return Path(__file__).resolve().parent / "logs" / "db.jsonl"

    @staticmethod
    def get_llm_provider() -> AnthropicProvider:
        return AnthropicProvider(
            api_key=ANTHROPIC_API_KEY,
            app_id=APP_ID,
        )

    def get_system_prompt(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "text",
                "text": build_base_prompt(),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": build_roles_context(self.skills),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def register_memory_store(self) -> MemoryStore:
        db_path = Path(__file__).resolve().parent / ".mash" / "data-agent.db"
        return SQLiteStore(str(db_path))

    def register_cached_files(self) -> List[str]:
        db_dir = Path(__file__).resolve().parent
        candidates = [
            db_dir / ".mash" / "plan.md",
            db_dir / "metrics-layer" / "index.yml",
            db_dir / "metrics-layer" / "schema" / "index.schema.yml",
            db_dir / "metrics-layer" / "schema" / "source.schema.yml",
            db_dir / "metrics-layer" / "schema" / "metric.schema.yml",
        ]
        return [str(path) for path in candidates if path.exists()]

    def register_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        local_tools = build_local_tools(
            store=self.store,
            app_id=self.app_id,
            get_session_id=lambda: getattr(self, "session_id", None),
            workspace_root=self.workspace_root,
        )
        for tool in local_tools:
            tools.register(tool)
        return tools

    def register_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skills_dir = Path(__file__).resolve().parent / ".mash" / "skills"
        for skill in skills.get_custom_skills(skills_dir):
            skills.register(skill)
        return skills

    def register_agent(self) -> Agent:
        llm = DataAgentApp.get_llm_provider()
        config = AgentConfig(
            app_id=self.app_id,
            system_prompt=self.get_system_prompt(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            conversation_history_turns=3,
            compaction_token_threshold=30000,
            skills_enabled=True,
            tool_search_enabled=False,
        )
        return Agent(
            llm=llm,
            tools=self.tools,
            skills=self.skills,
            config=config,
        )

    def _build_mcp_servers(self) -> List[Dict[str, Any]]:
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
            {
                "name": BIGQUERY_CONNECTION_NAME,
                "url": BIGQUERY_MCP_URL,
                "description": "BigQuery MCP server for data exploration",
                "headers": headers,
                "allowed_tools": BIGQUERY_ALLOWED_TOOLS,
            }
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

    def cleanup(self) -> None:
        if hasattr(self, "mcp_manager"):
            self.mcp_manager.disconnect_all()


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

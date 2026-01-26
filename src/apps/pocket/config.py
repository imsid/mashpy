"""Configuration helpers for the Pocket CLI."""

import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Pocket MCP server configuration
POCKET_MCP_URL = os.getenv(
    "POCKET_MCP_URL", "https://pocket-feed-mcp.onrender.com/mcp/pocket"
)
POCKET_MCP_TOKEN = os.getenv("POCKET_MCP_TOKEN", "aDbidFDN")

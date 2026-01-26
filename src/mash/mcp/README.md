# Mash MCP Integration

This module provides MCP (Model Context Protocol) integration for the Mash agent SDK.

## Overview

The MCP integration has been migrated from `src/mashnet` into `src/mash/mcp` and provides:

- **Host**: Manages MCP client instances and handles sampling/elicitation requests
- **MCPHTTPClient**: HTTP-based MCP client implementing the full protocol
- **MCPServer**: Wrapper for individual MCP server connections
- **MCPManager**: High-level manager for multiple MCP server connections

## Quick Start

### Basic Usage with MCPManager

```python
from mash.mcp import MCPManager

# Initialize manager
manager = MCPManager(default_model="gpt-4.1-mini")

# Add MCP servers
manager.add_server(
    name="my-server",
    url="https://example.com/mcp",
    description="Example MCP server",
    headers={"Authorization": "Bearer token"},
    allowed_tools=["tool1", "tool2"],  # Optional whitelist
    auto_connect=True
)

# List all tools from all servers
all_tools = manager.get_flattened_tools(prefix="mcp_")

# Call a tool
result = manager.call_tool(
    server_name="my-server",
    tool_name="tool1",
    arguments={"param": "value"}
)

# Clean up
manager.disconnect_all()
```

### Direct Usage with Host

```python
from mash.mcp import Host

# Initialize host
host = Host(default_model="gpt-4.1-mini")

# Get a client for an MCP server
client = host.get_client(
    url="https://example.com/mcp",
    name="Example Server",
    headers={"Authorization": "Bearer token"}
)

# List tools
tools = client.list_tools()

# Call a tool
result = client.call_tool("tool_name", {"arg": "value"})

# Clean up
host.close()
```

## Configuration

### Environment Variables

The MCP integration uses the following environment variables:

- `MASH_SAMPLING_MODEL`: Default model for sampling requests (preferred)
- `PLOG_SAMPLING_MODEL`: Fallback for backward compatibility
- `OPENAI_API_KEY`: OpenAI API key for sampling requests

You can also pass the default model directly:

```python
manager = MCPManager(default_model="gpt-4-turbo")
```

### Tool Filtering

You can restrict which tools are accessible from a server:

```python
manager.add_server(
    name="server",
    url="https://example.com/mcp",
    allowed_tools=["safe_tool1", "safe_tool2"]  # Only these tools can be called
)
```

## Components

### Host

The `Host` class manages MCP client instances and handles:
- Client connection pooling (cached by URL + headers)
- Sampling requests via OpenAI API
- Elicitation requests (user input prompts)

```python
from mash.mcp import Host

host = Host(default_model="gpt-4.1-mini")
client = host.get_client("https://server.com/mcp", "Server Name")
```

### MCPHTTPClient

Low-level MCP client implementing:
- MCP handshake (initialize, notifications/initialized)
- JSON-RPC over HTTP
- Server-Sent Events (SSE) for server-initiated messages
- Tool, resource, and prompt operations

```python
from mash.mcp import MCPHTTPClient, Host

host = Host()
client = host.get_client("https://server.com/mcp", "Server")

# List and call tools
tools = client.list_tools()
result = client.call_tool("tool_name", {"arg": "value"})

# Resources
resources = client.list_resources()
content = client.read_resource("resource://path")

# Prompts
prompts = client.list_prompts()
prompt = client.get_prompt("prompt_name", {"arg": "value"})
```

### MCPServer

Wrapper around `MCPHTTPClient` providing:
- Connection lifecycle management
- Tool whitelisting
- Higher-level API

```python
from mash.mcp import MCPServer, Host

host = Host()
server = MCPServer(
    name="my-server",
    url="https://server.com/mcp",
    allowed_tools=["tool1", "tool2"]
)

# Connect using host
client = host.get_client(server.url, server.name, server.headers)
server.connect(client)

# Use server
tools = server.list_tools()  # Only whitelisted tools
result = server.call_tool("tool1", {})
```

### MCPManager

High-level manager for multiple servers:
- Manages Host instance internally
- Server lifecycle (add, remove, connect, disconnect)
- Tool aggregation across servers
- Tool name prefixing and normalization

```python
from mash.mcp import MCPManager

manager = MCPManager()

# Add multiple servers
manager.add_server("server1", "https://s1.com/mcp")
manager.add_server("server2", "https://s2.com/mcp")

# Get all tools with prefixes
# Result: {"server1": [...], "server2": [...]}
tools_by_server = manager.get_all_tools(prefix="mcp_")

# Get flattened list
# Result: [{"name": "mcp_server1_tool1", ...}, {"name": "mcp_server2_tool2", ...}]
all_tools = manager.get_flattened_tools(prefix="mcp_")

# Call tools
manager.call_tool("server1", "tool_name", {"arg": "value"})
```

## Migration from mashnet

If you were using `mashnet` directly:

### Before (mashnet):
```python
from mashnet import Host

host = Host()
client = host.get_client("https://server.com/mcp", "Server")
```

### After (mash.mcp):
```python
from mash.mcp import Host

host = Host()
client = host.get_client("https://server.com/mcp", "Server")
```

The API is identical, just the import path has changed.

## Integration with Mash Tools

To convert MCP tools to Mash tool format:

```python
from mash.mcp import MCPManager
from mash.tools.mcp import MCPToolAdapter

manager = MCPManager()
manager.add_server("server", "https://server.com/mcp")

# Get MCP tools
mcp_tools = manager.get_flattened_tools()

# Convert to Mash tool adapters
for mcp_tool in mcp_tools:
    # Create executor that calls the right server
    server_name = mcp_tool["metadata"]["server"]
    original_name = mcp_tool["metadata"]["original_name"]

    def executor(args):
        return manager.call_tool(server_name, original_name, args)

    # Create adapter
    adapter = MCPToolAdapter.from_mcp_tool(
        mcp_tool=mcp_tool,
        executor=executor,
        prefix="mcp_"
    )
```

## Logging

All components use Python's standard logging:

- `mash.mcp.host`: Host-level operations
- `mash.mcp.client`: Client protocol operations
- `mash.mcp.manager`: Manager operations

Configure logging in your application:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("mash.mcp").setLevel(logging.DEBUG)
```

## Error Handling

```python
from mash.mcp import MCPManager, MCPClientError

manager = MCPManager()

try:
    manager.add_server("server", "https://invalid.com/mcp")
except MCPClientError as e:
    print(f"Connection failed: {e}")

try:
    result = manager.call_tool("server", "unknown_tool", {})
except RuntimeError as e:
    print(f"Tool call failed: {e}")
```

## Advanced: Sampling and Elicitation

MCP servers can request:
- **Sampling**: LLM completions via OpenAI API
- **Elicitation**: User input via stdin

These are handled automatically by the Host:

```python
from mash.mcp import Host

# Sampling uses OpenAI API with configured model
host = Host(default_model="gpt-4-turbo")

# Elicitation prompts user on stdin
# The host will display: "[elicitation] Server's question"
# And wait for user input
```

## Complete Example

```python
from mash.mcp import MCPManager, MCPClientError
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

# Initialize manager
manager = MCPManager(default_model="gpt-4.1-mini")

try:
    # Add servers
    manager.add_server(
        name="filesystem",
        url="https://mcp.example.com/fs",
        description="Filesystem access",
        allowed_tools=["read_file", "list_directory"]
    )

    manager.add_server(
        name="database",
        url="https://mcp.example.com/db",
        headers={"Authorization": "Bearer secret"},
        allowed_tools=["query"]
    )

    # Get all available tools
    print(f"Connected to {len(manager)} servers")

    for server_name in manager.list_servers():
        server = manager.get_server(server_name)
        tools = server.list_tools()
        print(f"{server_name}: {len(tools)} tools")

    # Call a tool
    result = manager.call_tool(
        server_name="filesystem",
        tool_name="read_file",
        arguments={"path": "/etc/hosts"}
    )
    print(f"Result: {result}")

except MCPClientError as e:
    print(f"MCP error: {e}")
finally:
    # Clean up
    manager.disconnect_all()
```

## mashnet

`mashnet` is the MCP transport layer. It handles the HTTP handshake,
streaming events, and tool/resource calls against remote MCP servers.

### Key components

- `MCPHTTPClient` performs the MCP initialize/initialized flow, listens for
  SSE events, and exposes helpers like `list_tools` and `call_tool`.
- `Host` manages MCP client instances and responds to sampling/elicitation
  requests from servers.

### Sampling and elicitation

`Host` currently fulfills sampling requests using OpenAI's chat completion
API. The default model is `gpt-4.1-mini`, but you can override it with
`PLOG_SAMPLING_MODEL`.

### Minimal usage

```python
from mashnet import Host

host = Host()
client = host.get_client("https://example.com/mcp", "Example")
tools = client.list_tools()
```

`mash` uses `mashnet` under the hood to populate its tool registry and
serve context to the agent runtime.

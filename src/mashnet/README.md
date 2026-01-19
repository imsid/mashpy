## mashnet

`mashnet` is the MCP transport layer. It handles the HTTP handshake,
streaming events, and tool/resource calls against remote MCP servers.

### Key components

- `MCPHTTPClient` performs the MCP initialize/initialized flow, listens for
  SSE events, and exposes helpers like `list_tools`, `call_tool`,
  `list_resources`, and `read_resource`.
- `Host` manages MCP client instances (cached by URL + headers) and responds to
  sampling/elicitation requests from servers.

### Sampling and elicitation

`Host` fulfills sampling requests using OpenAI's chat completion API via
`AsyncOpenAI`. The default model is `gpt-4.1-mini`, but you can override it with
`PLOG_SAMPLING_MODEL`, or by providing model hints in the sampling payload.

Elicitation requests are surfaced to the operator via stdout and the response
is collected from stdin before being sent back to the server.

`mashnet.host` loads environment variables from `.env` on import (via
`python-dotenv`), so `OPENAI_API_KEY` and related settings can be placed there.

### Logging

`MCPHTTPClient` and `Host` write simple log files alongside their modules:

- `src/mashnet/client.log`
- `src/mashnet/host.log`

### Minimal usage

```python
from mashnet import Host

host = Host()
client = host.get_client("https://example.com/mcp", "Example")
tools = client.list_tools()
```

`mash` uses `mashnet` under the hood to populate its tool registry and
serve context to the agent runtime.

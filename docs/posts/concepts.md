# Core concepts

An agent is a language model combined with tools, memory, and instructions to work through a task across multiple steps. Each component below plays a specific role in that system.

**Model**

A large language model (LLM) trained on large amounts of text. Models are trained once; the result is a fixed set of weights that determines how the model responds.

**Context window**

The information loaded into a model at runtime. It augments what the model was trained on and gives it what it needs to complete the task at hand. Measured in tokens: input tokens are what goes in, output tokens are what the model produces. Most frontier models today support around one million tokens, and keeping context within that limit is a core constraint in any agent system.

**Prompt**

The initial set of instructions loaded into the context window. It describes the model's role, responsibilities, and guardrails. The prompt is static and loaded once at the start of each agent run.

**Tool**

An executable piece of code the model can call. Tools are registered at the start of the agent run and the model decides which ones to invoke based on the task. Tools can be local, operating within the same environment (reading or writing to the filesystem, for example), or remote, communicating with an external service like a CRM or data warehouse. Remote tools are packaged inside an MCP (Model Context Protocol) server and typically require authorization. Mash ships a set of built-in local tools and an interface for registering custom local and remote MCP server tools.

**Skill**

A markdown file containing instructions for a specific task. Each skill has a short frontmatter description and the full instructions below it. Only the frontmatter is loaded into the context window at startup; the model pulls in the full instructions only when the task calls for them. This is called progressive disclosure. A skill has no code to execute; it is instructions the model reads and follows. Skills live in a folder that gets registered with Mash.

**Memory**

Where conversations are stored across sessions. In Mash, memory lives in a Postgres table. Each row holds a user message and the model's response. The model can reach into memory through a tool to retrieve information from past sessions.

**Conversation**

Models are stateless. To preserve continuity within a session, the back-and-forth between user and model is chained together and loaded into the context window. Long conversations can exceed the context window limit, so they need to be capped. Mash defaults to the most recent three turns. For longer sessions, conversations can be compacted: summarized into a shorter form once they pass a token or turn threshold.

**Provider**

An inference provider that hosts and serves a model. Each provider exposes its own API. Mash supports OpenAI, Anthropic, and Gemini out of the box. Open-source models including Gemma, Qwen, DeepSeek, and Llama can be served locally or through a hosted gateway like OpenRouter. Mash ships a general-purpose provider that works with any endpoint supporting the OpenAI Chat Completions API.

**Agent**

A specification that combines a prompt, tools, skills, memory, and a provider. Together they give the model what it needs to work through a large, multi-step task. Each step in the process is a turn; the full sequence is the agent loop. Individual turns can run autonomously or pause for human input. Because the loop can be long-running and run in the background, durability matters: retries, resumability, replays, and observability.

Every agent framework offers its own way to define an agent spec and execute the loop. Mash provides a Python SDK for authoring agents and uses `dbos-transact` for durable execution.

To make the spec concrete, here is what a Morning Brief agent looks like, one that assembles a personalized daily digest:

- **Prompt**: the agent's role and the brief format it should produce
- **Tools**: `web_search` to pull local news, `slack_digest` to fetch unread messages, `rss_feed` to check for new episode drops from subscribed creators
- **Skill**: instructions for curating the brief, including how to weight topics by the past seven days of engagement data and how to lay out the output
- **Memory**: stores past briefs and engagement signals so each new brief can reflect what the user has actually read and followed up on
- **Provider**: the model provider of your choice

**Events**

Events let you observe the agent loop while it is running. Each event has a defined schema and is recorded in the database. Common events include tool calls, model invocations, and terminal states. Events are sequential and ordered, which underpins the durable execution model. Mash uses a taxonomy of 11 events, covered in [Everything is an event](request-lifecycle.md#everything-is-an-event).

**Host**

A host is a pool of agents composed together. It is the bridge between a user application and one or more agents. Within a host, agents are assigned roles: **primary**, **subagent**, and **workflow**. The primary agent handles free-form user requests and can delegate to subagents. Workflow agents run a deterministic sequence of tasks and return structured output.

Host is a Mash-only concept for structuring how applications interface with agents. The interaction model between user application, host, and agents is governed by the [H2A Protocol](../rfcs/host-to-agent-protocol.md). Composing agents through a host means you can add or remove agents without changing the application. Hosts are self-hosted services, accessible over HTTP or via the CLI.

**Mash**
Python SDK for building agents, a runtime for deploying and managing a Host, and exposes an interface for embedding agents into any application.

---
name: mash-quiz
description: Interactive quiz about Mash SDK internals for learning.
---

# Mash Quiz

You are running an interactive quiz to help users learn about the Mash SDK.
This is not a competition — it's a guided learning experience to help users
understand Mash internals and overcome the cold-start problem.

## Background Context

Mash is an open-source SDK for building multi-agent applications in Python.
Key concepts users should learn about:

- **AgentSpec**: Defines an agent's identity, tools, skills, LLM provider,
  and system prompt
- **HostBuilder / AgentPool**: Registers role-less agents (each with
  AgentMetadata), workflows, and Host compositions into an AgentPool
- **Host**: Composition over the pool naming a primary and subagents;
  roles live in the host, not on the agents
- **AgentRuntime / RequestEngine**: Manages request lifecycle, event sourcing,
  and durable execution
- **Workflows**: DBOS-backed step pipelines with WorkflowSpec, CodeStep,
  AgentStep, and durable resume
- **Skills**: Markdown-based instructions loaded via the Skill meta-tool;
  can be static or dynamically registered
- **MCP Integration**: Connect to external tool servers via MCPServerConfig
- **Tools**: FunctionTool base class, ToolRegistry, AskUserTool, BashTool
- **LLM Providers**: Anthropic, OpenAI, Gemini — pluggable via build_llm()

Popular agent frameworks for comparison: LangChain, CrewAI, AutoGen,
OpenAI Agents SDK. Mash differentiates with durable workflows, event
sourcing, and a skill-driven architecture.

You have cached docs from every major Mash module loaded into your context.
Use them as the primary source of truth for crafting questions. Use Bash only
for narrow verification or to look up specific implementation details that
the cached docs do not cover.

## Using AskUser

AskUser supports two interaction types. Choose the right one for each question:

### Free-form (`info`)

Call AskUser with only a `question` parameter — no `options`. The user types
a free-text response. Use this for open-ended questions, follow-up
discussion, or when the answer requires explanation rather than selection.

```
AskUser(question="What happens when a workflow task agent calls AskUser mid-execution?")
```

### Multiple choice (`choice`)

Call AskUser with both `question` and `options` parameters. The user selects
from the provided list. Use this for conceptual questions where there are
clear distinct answers to choose from. Always use exactly 3 options.

```
AskUser(
    question="Which method on AgentSpec defines the tools available to an agent?",
    options=["build_tools()", "build_skills()", "get_agent_id()"]
)
```

After AskUser returns, check `metadata.timed_out` — if true, the user did
not respond in time. Handle gracefully by moving on.

## Quiz Flow

1. Review the cached docs in your context to identify interesting topics
   across different Mash modules. Use one or two targeted Bash commands only
   if you need to verify a specific detail (e.g. confirm a function signature
   or check a file path).
2. Generate 3 questions in increasing order of complexity:
   - **Easy**: A multiple-choice (`options`) question about a core Mash
     concept — good for warming up
   - **Medium**: A question requiring understanding of how components interact.
     Can be multiple-choice or free-form depending on the topic
   - **Hard**: A free-form question about implementation details, edge cases,
     or design tradeoffs that requires the user to explain their reasoning
3. Present each question one at a time using AskUser.
4. After the user responds, deliver feedback **inside the next AskUser call**,
   not as standalone assistant text. Prepend the feedback (correct/incorrect,
   explanation, code references) to the next question's `question` string,
   separated by a horizontal rule (`---`). For the final question, deliver
   feedback as your final response text (no further AskUser needed).
   This ensures feedback is always visible to the user — assistant text
   between tool calls is rendered as dim trace output and easy to miss.
5. If the user asks a follow-up question about the answer, answer it
   thoroughly before moving to the next quiz question. Use AskUser (free-form)
   to prompt for follow-ups when the user's answer suggests confusion.
6. **Early exit**: if any AskUser response indicates the user wants to stop
   or has exited (e.g. "stop", "quit", "exit", or a message saying the user
   exited or interrupted the quiz), end the quiz immediately. Do not call
   AskUser again and do not ask remaining questions — return a one-line
   goodbye as your final response text.

## Guidelines

- Draw questions primarily from the cached docs — they cover core, tools,
  skills, runtime, workflows, mcp, cli, api, and masher
- Vary topics across the 3 questions (don't ask 3 questions about the same
  module)
- Frame questions as learning opportunities, not trick questions
- Keep explanations concise but informative
- Reference specific files and functions when explaining answers
- Use multiple-choice (exactly 3 options) for questions with clear distinct
  answers; use free-form for questions that benefit from the user articulating
  their understanding

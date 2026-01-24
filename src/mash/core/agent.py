"""Core agent execution loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .config import AgentConfig
from .context import Action, ActionType, Context, MessageRole, Response, ToolResult
from .llm import LLMProvider

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry
    from ..memory.signals import SignalCollector
    from ..memory.ranker import ExampleRanker


class Agent:
    """Agent that executes the think-act-observe loop."""

    def __init__(
        self,
        llm: LLMProvider,
        tools: ToolRegistry,
        config: AgentConfig,
    ) -> None:
        """Initialize the agent.

        Args:
            llm: LLM provider for generating responses.
            tools: Tool registry containing available tools.
            config: Agent configuration.
        """
        self.llm = llm
        self.tools = tools
        self.config = config
        self._signal_collector: Optional[SignalCollector] = None
        self._ranker: Optional[ExampleRanker] = None

    def set_signal_collector(self, collector: SignalCollector) -> None:
        """Set the signal collector for automatic signal collection."""
        self._signal_collector = collector

    def set_ranker(self, ranker: ExampleRanker) -> None:
        """Set the example ranker for learning from past interactions."""
        self._ranker = ranker

    def run(self, context: Context) -> Response:
        """Execute the agent loop.

        Args:
            context: Execution context with messages and state.

        Returns:
            Response containing the agent's output.
        """
        for step in range(self.config.max_steps):
            # Think: decide what to do next
            action = self.think(context)

            # Check if we're done
            if action.type == ActionType.FINISH:
                context.mark_complete()
                break

            # Act: execute the action
            results = self.act(action)

            # Observe: update context with results
            context = self.observe(context, action, results)

            # Collect signals (automatic)
            if self._signal_collector:
                signals = self._collect_signals(context, action, results)
                context.signals.update(signals)

        return Response.from_context(context)

    def think(self, context: Context) -> Action:
        """Decide what action to take.

        Args:
            context: Current execution context.

        Returns:
            Action to take.
        """
        # Get tool definitions
        tool_defs = self.tools.to_llm_format()

        # Get messages for LLM
        messages = context.get_messages_for_llm()

        # Enhance system prompt with examples if ranker is available
        system_prompt = context.system_prompt
        if self._ranker and messages:
            # Get the last user message as query
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    examples = self._ranker.get_best_examples(
                        query=msg.get("content", ""),
                        limit=3,
                    )
                    if examples:
                        example_text = self._format_examples(examples)
                        system_prompt = f"{system_prompt}\n\n{example_text}"
                    break

        # Call LLM
        response = self.llm.create_message(
            model=self.config.model,
            system=system_prompt,
            messages=messages,
            tools=tool_defs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )

        # Parse response
        text, tool_calls, blocks = self.llm.parse_response(response)

        # Store the assistant's response in context
        if blocks:
            # Store as content blocks for proper format
            context.messages.append(
                {
                    "role": "assistant",
                    "content": blocks,
                }
            )

        # Determine action type
        if tool_calls:
            return Action.from_tool_calls(tool_calls)
        elif text:
            return Action.from_response(text)
        else:
            return Action.finish()

    def act(self, action: Action) -> List[ToolResult]:
        """Execute an action.

        Args:
            action: Action to execute.

        Returns:
            List of tool results.
        """
        if action.type != ActionType.TOOL_CALL:
            return []

        results: List[ToolResult] = []
        for tool_call in action.tool_calls:
            try:
                tool = self.tools.get(tool_call.name)
                if tool is None:
                    results.append(
                        ToolResult(
                            tool_call_id=tool_call.id,
                            content=f"Error: Tool '{tool_call.name}' not found",
                            is_error=True,
                        )
                    )
                    continue

                # Execute the tool
                result = tool.execute(tool_call.arguments)
                results.append(
                    ToolResult(
                        tool_call_id=tool_call.id,
                        content=result.content,
                        is_error=result.is_error,
                        metadata=result.metadata,
                    )
                )
            except Exception as e:
                results.append(
                    ToolResult(
                        tool_call_id=tool_call.id,
                        content=f"Error executing tool: {str(e)}",
                        is_error=True,
                    )
                )

        return results

    def observe(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
    ) -> Context:
        """Update context with action results.

        Args:
            context: Current context.
            action: Action that was taken.
            results: Results from executing the action.

        Returns:
            Updated context.
        """
        if action.type == ActionType.TOOL_CALL and results:
            # Add tool results to context in proper format
            tool_result_blocks = [result.to_dict() for result in results]
            context.messages.append(
                {
                    "role": "user",
                    "content": tool_result_blocks,
                }
            )

        return context

    def _collect_signals(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
    ) -> Dict[str, Any]:
        """Collect signals from the current step.

        Args:
            context: Current context.
            action: Action that was taken.
            results: Results from executing the action.

        Returns:
            Dictionary of collected signals.
        """
        if not self._signal_collector:
            return {}

        event = {
            "context": context,
            "action": action,
            "results": results,
        }

        return self._signal_collector.collect(event)

    def _format_examples(self, examples: List[Dict[str, Any]]) -> str:
        """Format examples for inclusion in system prompt.

        Args:
            examples: List of high-quality examples.

        Returns:
            Formatted string for system prompt.
        """
        if not examples:
            return ""

        lines = ["EXAMPLES FROM HIGH-PERFORMING INTERACTIONS:"]
        for i, example in enumerate(examples, 1):
            lines.append(f"\nExample {i}:")
            lines.append(f"Query: {example.get('user_message', '')}")
            lines.append(f"Response: {example.get('agent_response', '')}")

        return "\n".join(lines)

#!/bin/bash
# View Tool Calls with Commands
#
# This script shows all tool calls in a trace with their arguments (especially bash commands)

set -e

LOG_FILE="$HOME/.mash/logs/codebase_v2.jsonl"

if [ -n "$1" ]; then
    TRACE_ID="$1"
    echo "Viewing tool calls for trace: $TRACE_ID"
else
    # Get most recent trace
    TRACE_ID=$(tail -100 "$LOG_FILE" | jq -r 'select(.event_type=="agent.run.start") | .trace_id' | tail -1)

    if [ -z "$TRACE_ID" ]; then
        echo "❌ No recent traces found."
        echo ""
        echo "Usage:"
        echo "  $0                  # View most recent trace"
        echo "  $0 <trace_id>       # View specific trace"
        exit 1
    fi

    echo "Viewing most recent trace: $TRACE_ID"
fi

echo ""
echo "=========================================="
echo "TOOL CALLS"
echo "=========================================="
echo ""

# Get user query
USER_QUERY=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.run.start") | .payload.user_message' | \
    head -1)

echo "User Query: $USER_QUERY"
echo ""

# Get all tool call events
TOOL_CALLS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "agent.tool.call")')

if [ -z "$TOOL_CALLS" ]; then
    echo "⚠️  No tool calls found for this trace."
    echo ""
    echo "Note: Tool call logging was added in the latest update."
    echo "      Old traces won't have agent.tool.call events."
    exit 0
fi

# Parse and display each tool call
CALL_NUM=0
echo "$TOOL_CALLS" | while read -r line; do
    CALL_NUM=$((CALL_NUM + 1))

    TOOL_NAME=$(echo "$line" | jq -r '.payload.tool_name')
    TOOL_ARGS=$(echo "$line" | jq -c '.payload.tool_arguments')
    TIMESTAMP=$(echo "$line" | jq -r '.ts')

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Tool Call #$CALL_NUM"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Tool: $TOOL_NAME"
    echo "Time: $(date -r $TIMESTAMP '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$TIMESTAMP" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo $TIMESTAMP)"
    echo ""

    # Special handling for bash tool
    if [ "$TOOL_NAME" = "bash" ]; then
        COMMAND=$(echo "$TOOL_ARGS" | jq -r '.command')
        echo "Command:"
        echo "  $COMMAND"
    else
        echo "Arguments:"
        echo "$TOOL_ARGS" | jq '.'
    fi

    echo ""
done

echo "=========================================="
echo "TOOL RESULTS"
echo "=========================================="
echo ""

# Get all tool result events
TOOL_RESULTS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "agent.tool.result")')

if [ -z "$TOOL_RESULTS" ]; then
    echo "⚠️  No tool results logged."
    exit 0
fi

RESULT_NUM=0
echo "$TOOL_RESULTS" | while read -r line; do
    RESULT_NUM=$((RESULT_NUM + 1))

    TOOL_NAME=$(echo "$line" | jq -r '.payload.tool_name')
    IS_ERROR=$(echo "$line" | jq -r '.payload.is_error')
    CONTENT_LEN=$(echo "$line" | jq -r '.payload.content_length')
    PREVIEW=$(echo "$line" | jq -r '.payload.content_preview')

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Tool Result #$RESULT_NUM"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Tool: $TOOL_NAME"
    echo "Error: $IS_ERROR"
    echo "Output Size: $CONTENT_LEN chars"
    echo ""

    if [ -n "$PREVIEW" ] && [ "$PREVIEW" != "null" ]; then
        echo "Preview (first 200 chars):"
        echo "$PREVIEW"
        echo ""
    fi
done

echo "=========================================="
echo ""

# Summary
TOTAL_CALLS=$(echo "$TOOL_CALLS" | wc -l | tr -d ' ')
BASH_CALLS=$(echo "$TOOL_CALLS" | jq -r 'select(.payload.tool_name == "bash")' | wc -l | tr -d ' ')

echo "Summary:"
echo "  Total tool calls: $TOTAL_CALLS"
echo "  Bash calls: $BASH_CALLS"
echo ""

# Check for search-first strategy
FIRST_TOOL=$(echo "$TOOL_CALLS" | head -1 | jq -r '.payload.tool_name')
if [ "$FIRST_TOOL" = "bash" ]; then
    FIRST_COMMAND=$(echo "$TOOL_CALLS" | head -1 | jq -r '.payload.tool_arguments.command')

    if echo "$FIRST_COMMAND" | grep -qE "rg|grep"; then
        echo "✅ Agent used search-first strategy (first command: rg/grep)"
    else
        echo "⚠️  Agent did NOT use search-first strategy"
        echo "   First command: $FIRST_COMMAND"
    fi
fi

echo ""

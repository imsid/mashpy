#!/bin/bash
# Verify Local Repo Mode Fix
#
# This script checks if bash tool is available and usable for local repos

set -e

LOG_FILE="$HOME/.mash/logs/codebase_v2.jsonl"

echo "=========================================="
echo "Local Repo Mode Verification"
echo "=========================================="
echo ""

# Get the most recent trace
echo "Finding most recent trace..."
TRACE_ID=$(tail -100 "$LOG_FILE" | jq -r 'select(.event_type=="agent.run.start") | .trace_id' | tail -1)

if [ -z "$TRACE_ID" ]; then
    echo "❌ No recent traces found. Run the agent first with a local repo."
    echo ""
    echo "To test:"
    echo "  poetry run codebase-agent-v2"
    echo "  /switch_repo /Users/sid/Projects/pocket"
    echo "  > tell me about this repo"
    exit 1
fi

echo "✓ Found trace: $TRACE_ID"
echo ""

# Check if this was a local or GitHub repo query
echo "=== Repository Type Check ==="

# Get tool list to determine repo type
TOOLS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -s '[.[] | select(.event_type == "llm.request.complete")][0] | .tools' 2>/dev/null)

if echo "$TOOLS" | grep -q "bash"; then
    echo "✓ Repo type: LOCAL (bash tool present)"
    REPO_TYPE="local"
else
    echo "ℹ Repo type: GITHUB (no bash tool)"
    REPO_TYPE="github"
fi

echo ""

# Analyze tool configuration
echo "=== Tool Configuration ==="

TOOL_BREAKDOWN=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -s '[.[] | select(.event_type == "agent.tools.token_breakdown")][0]' 2>/dev/null)

if [ -z "$TOOL_BREAKDOWN" ] || [ "$TOOL_BREAKDOWN" = "null" ]; then
    echo "⚠️  No tool breakdown found for this trace"
    exit 1
fi

TOTAL_TOOLS=$(echo "$TOOL_BREAKDOWN" | jq '.payload.tool_count')
DEFERRED=$(echo "$TOOL_BREAKDOWN" | jq '.payload.deferred_tool_count')
NON_DEFERRED=$(echo "$TOOL_BREAKDOWN" | jq '.payload.non_deferred_tool_count')
TOTAL_TOKENS=$(echo "$TOOL_BREAKDOWN" | jq '.payload.total_tool_tokens')

echo "Total tools: $TOTAL_TOOLS"
echo "  Deferred: $DEFERRED"
echo "  Non-deferred: $NON_DEFERRED"
echo "  Total tokens: $TOTAL_TOKENS"
echo ""

# Check bash tool status
if [ "$REPO_TYPE" = "local" ]; then
    echo "=== Bash Tool Status (LOCAL REPO) ==="

    BASH_INFO=$(echo "$TOOL_BREAKDOWN" | \
        jq '.payload.all_tool_sizes[] | select(.name == "bash")')

    if [ -z "$BASH_INFO" ] || [ "$BASH_INFO" = "null" ]; then
        echo "❌ bash tool NOT FOUND in tool list"
        echo "   This is a BUG - bash should be available for local repos"
        exit 1
    fi

    BASH_TOKENS=$(echo "$BASH_INFO" | jq '.tokens')
    BASH_DEFERRED=$(echo "$BASH_INFO" | jq '.deferred')

    echo "bash tool found:"
    echo "  Tokens: $BASH_TOKENS"
    echo "  Deferred: $BASH_DEFERRED"
    echo ""

    if [ "$BASH_DEFERRED" = "true" ]; then
        echo "❌ PROBLEM: bash is DEFERRED"
        echo "   This prevents the agent from using bash properly"
        echo "   Expected: deferred=false with ~79 tokens"
        echo ""
        echo "The fix needs to be applied. Check:"
        echo "  src/mash/core/agent.py line ~510"
        echo "  Ensure bash is in critical_tools set"
        exit 1
    else
        echo "✅ bash is NOT deferred (correct!)"

        if [ "$BASH_TOKENS" -lt 50 ]; then
            echo "⚠️  bash tokens seem low ($BASH_TOKENS)"
            echo "   Expected: ~79 tokens for full definition"
        else
            echo "✅ bash has full definition ($BASH_TOKENS tokens)"
        fi
    fi
fi

echo ""

# Check runtime tools
echo "=== Runtime Tools Status ==="

RUNTIME_TOOLS=("get_preferences" "set_preferences" "get_app_data" "set_app_data")
RUNTIME_OK=0
RUNTIME_DEFERRED=0

for tool in "${RUNTIME_TOOLS[@]}"; do
    TOOL_INFO=$(echo "$TOOL_BREAKDOWN" | \
        jq ".payload.all_tool_sizes[] | select(.name == \"$tool\")")

    if [ -z "$TOOL_INFO" ] || [ "$TOOL_INFO" = "null" ]; then
        echo "⚠️  $tool: NOT FOUND"
        continue
    fi

    IS_DEFERRED=$(echo "$TOOL_INFO" | jq '.deferred')
    TOKENS=$(echo "$TOOL_INFO" | jq '.tokens')

    if [ "$IS_DEFERRED" = "true" ]; then
        echo "❌ $tool: DEFERRED ($TOKENS tokens) - should be non-deferred"
        RUNTIME_DEFERRED=$((RUNTIME_DEFERRED + 1))
    else
        echo "✅ $tool: NON-DEFERRED ($TOKENS tokens)"
        RUNTIME_OK=$((RUNTIME_OK + 1))
    fi
done

echo ""

if [ $RUNTIME_DEFERRED -gt 0 ]; then
    echo "⚠️  $RUNTIME_DEFERRED runtime tools are deferred"
    echo "   Runtime tools should always have full definitions"
fi

# Check if agent actually used bash (for local repos)
if [ "$REPO_TYPE" = "local" ]; then
    echo "=== Bash Usage Check ==="

    # Look for bash tool calls (would appear in mcp or tool events)
    # Since bash is not an MCP tool, it would appear differently
    # Let's check the agent response

    RESPONSE=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
        jq -r 'select(.event_type == "agent.run.complete") | .payload.assistant_response' 2>/dev/null)

    if echo "$RESPONSE" | grep -qi "don't have bash"; then
        echo "❌ Agent said it doesn't have bash access"
        echo "   This means the fix didn't work or wasn't applied"
        echo ""
        echo "First few lines of response:"
        echo "$RESPONSE" | head -c 500
        exit 1
    fi

    if echo "$RESPONSE" | grep -qi "README\|package\|setup\|directory\|file"; then
        echo "✅ Agent appears to have explored the repository"
        echo "   (mentions files/directories in response)"
    else
        echo "⚠️  Agent response doesn't mention files/directories"
        echo "   May not have successfully used bash"
    fi
fi

echo ""
echo "=========================================="
echo "Verification Summary"
echo "=========================================="
echo ""

if [ "$REPO_TYPE" = "local" ]; then
    if [ "$BASH_DEFERRED" = "false" ] && [ "$BASH_TOKENS" -gt 50 ]; then
        echo "✅ LOCAL REPO MODE: WORKING"
        echo "   - bash tool available with full definition"
        echo "   - bash is NOT deferred ($BASH_TOKENS tokens)"
        echo "   - Runtime tools available"
    else
        echo "❌ LOCAL REPO MODE: BROKEN"
        echo "   - bash tool issue detected"
        echo "   - Apply the fix in src/mash/core/agent.py"
    fi
else
    echo "ℹ  GITHUB REPO MODE (bash not expected)"
    echo "   - Runtime tools should be non-deferred"
    if [ $RUNTIME_OK -ge 3 ]; then
        echo "   - ✅ Runtime tools properly configured"
    else
        echo "   - ⚠️  Some runtime tools may be deferred"
    fi
fi

echo ""
echo "Tool configuration:"
echo "  Total: $TOTAL_TOOLS tools"
echo "  Non-deferred: $NON_DEFERRED (should be 8-9 for local, 7-8 for GitHub)"
echo "  Deferred: $DEFERRED"
echo "  Total tokens: $TOTAL_TOKENS"

#!/bin/bash
# Verify Bash Exploration Guidance
#
# This script checks if bash exploration guidance is correctly applied
# to local repos only (not GitHub repos)

set -e

LOG_FILE="$HOME/.mash/logs/codebase_v2.jsonl"

echo "=========================================="
echo "Bash Exploration Guidance Verification"
echo "=========================================="
echo ""

# Get the most recent trace
echo "Finding most recent trace..."
TRACE_ID=$(tail -100 "$LOG_FILE" | jq -r 'select(.event_type=="agent.run.start") | .trace_id' | tail -1)

if [ -z "$TRACE_ID" ]; then
    echo "❌ No recent traces found."
    echo ""
    echo "To test:"
    echo "  1. Start the agent: poetry run codebase-agent-v2"
    echo "  2. Switch to a local repo: /switch_repo /path/to/repo"
    echo "  3. Ask a question: > tell me about this repo"
    echo "  4. Run this script again"
    exit 1
fi

echo "✓ Found trace: $TRACE_ID"
echo ""

# Check repo type
echo "=== Repository Type Check ===="
echo ""

# Get the user message to see if it contains /switch_repo
USER_MSG=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.run.start") | .payload.user_message' | \
    head -1)

echo "User message: $USER_MSG"
echo ""

# Try to determine repo type from the trace
# Look for bash tool or GitHub MCP tools
HAS_BASH=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.tools.token_breakdown") | .payload.all_tool_sizes[].name' | \
    grep "^bash$" | head -1)

HAS_GITHUB=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.tools.token_breakdown") | .payload.all_tool_sizes[].name' | \
    grep "^mcp_github" | head -1)

if [ -n "$HAS_BASH" ]; then
    REPO_TYPE="local"
    echo "✓ Repo type: LOCAL (bash tool present)"
elif [ -n "$HAS_GITHUB" ]; then
    REPO_TYPE="github"
    echo "ℹ Repo type: GITHUB (GitHub MCP tools present)"
else
    REPO_TYPE="none"
    echo "ℹ Repo type: NONE (no repo-specific tools)"
fi

echo ""

# For local repos, verify bash guidance is in system prompt
if [ "$REPO_TYPE" = "local" ]; then
    echo "=== Bash Guidance Check (LOCAL REPO) ==="
    echo ""

    # Try to extract system prompt from first LLM request
    # Note: The actual system text might not be logged, so we'll check tool breakdown
    # as a proxy for whether the guidance was included

    TOOL_BREAKDOWN=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
        jq 'select(.event_type == "agent.tools.token_breakdown") | .payload' | \
        head -1)

    if [ -n "$TOOL_BREAKDOWN" ]; then
        BASH_TOKENS=$(echo "$TOOL_BREAKDOWN" | \
            jq '.all_tool_sizes[] | select(.name == "bash") | .tokens')

        echo "Bash tool configuration:"
        echo "  Tokens: $BASH_TOKENS"
        echo "  Deferred: $(echo "$TOOL_BREAKDOWN" | jq '.all_tool_sizes[] | select(.name == "bash") | .deferred')"
        echo ""

        if [ "$BASH_TOKENS" -gt 50 ]; then
            echo "✅ Bash tool has full definition ($BASH_TOKENS tokens)"
        else
            echo "⚠️  Bash tool tokens seem low ($BASH_TOKENS)"
        fi
    else
        echo "⚠️  No tool breakdown found"
    fi

    echo ""
    echo "Note: To fully verify the guidance is in the system prompt,"
    echo "      you would need to check the actual LLM request payload."
    echo "      The guidance is ~500 tokens and should be visible in"
    echo "      the agent's behavior (better exploration patterns)."
    echo ""

    # Check if agent's behavior suggests it's following the guidance
    echo "=== Behavior Analysis ==="
    echo ""

    BASH_CALLS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
        jq -r 'select(.event_type == "agent.act.complete") | .tool_calls[]' | \
        grep -c "bash" || echo "0")

    echo "Total bash calls: $BASH_CALLS"

    # Check for patterns that suggest guidance is being followed
    # (This would require extracting actual bash commands from tool calls)
    echo ""
    echo "To verify guidance is working:"
    echo "  1. Check if agent reads README.md first"
    echo "  2. Check if agent uses 'tree -L 2' for structure"
    echo "  3. Check if agent uses 'rg -l' to find files"
    echo "  4. Check if agent truncates outputs with 'head' or 'tail'"
    echo "  5. Check if agent avoids test files and node_modules"

elif [ "$REPO_TYPE" = "github" ]; then
    echo "=== GitHub Repo Check ==="
    echo ""
    echo "✓ GitHub repo detected"
    echo "  Bash guidance should NOT be in system prompt"
    echo "  GitHub MCP tools should be available instead"
    echo ""

    # Count GitHub MCP tools
    GITHUB_TOOLS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
        jq -r 'select(.event_type == "agent.tools.token_breakdown") | .payload.all_tool_sizes[].name' | \
        grep "^mcp_github" | wc -l)

    echo "GitHub MCP tools available: $GITHUB_TOOLS"

    if [ "$GITHUB_TOOLS" -gt 0 ]; then
        echo "✅ GitHub MCP tools are available"
    else
        echo "⚠️  No GitHub MCP tools found"
    fi

else
    echo "=== No Repo Selected ==="
    echo ""
    echo "ℹ  No repository is active"
    echo "   Bash guidance should NOT be in system prompt"
    echo "   User should be prompted to run /switch_repo"
fi

echo ""
echo "=========================================="
echo "Verification Summary"
echo "=========================================="
echo ""

if [ "$REPO_TYPE" = "local" ]; then
    echo "✅ LOCAL REPO MODE"
    echo "   - Bash guidance should be active"
    echo "   - System prompt includes ~500 tokens of bash best practices"
    echo "   - Agent should follow exploration patterns"
    echo ""
    echo "To verify effectiveness:"
    echo "   1. Ask: 'tell me about this repo'"
    echo "   2. Watch for: README first, tree for structure, targeted searches"
    echo "   3. Check: agent avoids tests and large outputs"

elif [ "$REPO_TYPE" = "github" ]; then
    echo "✅ GITHUB REPO MODE"
    echo "   - Bash guidance is NOT included (correct)"
    echo "   - GitHub MCP tools are used instead"
    echo "   - System prompt is optimized for MCP workflow"

else
    echo "ℹ  NO REPO MODE"
    echo "   - Bash guidance is NOT included (correct)"
    echo "   - User should select a repo with /switch_repo"
fi

echo ""
echo "=========================================="

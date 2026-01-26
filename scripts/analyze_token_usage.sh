#!/bin/bash
# Token Usage Analysis Script
#
# Usage: ./scripts/analyze_token_usage.sh <trace_id>
#
# Analyzes token usage for a specific trace by querying the logs.

set -e

TRACE_ID="$1"
LOG_FILE="$HOME/.mash/logs/codebase_v2.jsonl"

if [ -z "$TRACE_ID" ]; then
    echo "Usage: $0 <trace_id>"
    echo ""
    echo "Recent traces:"
    tail -100 "$LOG_FILE" | jq -r 'select(.trace_id) | .trace_id' | sort -u | tail -5
    exit 1
fi

echo "=========================================="
echo "Token Usage Analysis for Trace: $TRACE_ID"
echo "=========================================="
echo ""

# 1. Overall token usage from LLM
echo "=== LLM Token Usage ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "llm.request.complete") | {
        step: (.ts | tostring | split(".")[0]),
        input_tokens,
        output_tokens,
        cache_creation_tokens: .cache_creation_input_tokens,
        cache_read_tokens: .cache_read_input_tokens,
        total: (.input_tokens + .output_tokens)
    }'
echo ""

# 2. Cache performance
echo "=== Prompt Cache Performance ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "llm.request.complete") | {
        cache_enabled: (if .cache_creation_input_tokens or .cache_read_input_tokens then true else false end),
        cache_writes: .cache_creation_input_tokens,
        cache_reads: .cache_read_input_tokens,
        uncached_input: (.input_tokens - ((.cache_creation_input_tokens // 0) + (.cache_read_input_tokens // 0)))
    }' | head -1
echo ""

# 3. Prompt token breakdown
echo "=== Prompt Component Breakdown ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "agent.prompt.token_breakdown") | .payload | {
        system_prompt_tokens,
        tool_definitions_tokens,
        tool_count,
        messages_tokens,
        message_count,
        estimated_total_tokens,
        breakdown: {
            system_prompt_pct: ((.system_prompt_tokens * 100) / .estimated_total_tokens | floor),
            tools_pct: ((.tool_definitions_tokens * 100) / .estimated_total_tokens | floor),
            messages_pct: ((.messages_tokens * 100) / .estimated_total_tokens | floor)
        }
    }'
echo ""

# 3. Tool search status
echo "=== Tool Search Status ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -c 'select(.event_type == "agent.tools.token_breakdown") | {
        tool_search_enabled: .payload.tool_search_enabled,
        total_tools: .payload.tool_count,
        deferred_tools: .payload.deferred_tool_count,
        non_deferred_tools: .payload.non_deferred_tool_count,
        total_tool_tokens: .payload.total_tool_tokens
    }'
echo ""

# 4. Top 10 largest tools
echo "=== Top 10 Largest Tools ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.tools.token_breakdown") |
        .payload.top_10_largest_tools[] |
        if .deferred then
            "\(.name): \(.tokens) tokens (deferred)"
        else
            "\(.name): \(.tokens) tokens"
        end' | head -10
echo ""

# 5. Examples usage (if any)
echo "=== Examples from Ranker ==="
if grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | grep -q "agent.examples.added"; then
    grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
        jq -c 'select(.event_type == "agent.examples.added") | .payload'
else
    echo "(No examples added)"
fi
echo ""

# 6. Summary statistics
echo "=== Summary ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -s '[.[] | select(.event_type == "llm.request.complete")] | {
        total_steps: length,
        total_input_tokens: (map(.input_tokens) | add),
        total_output_tokens: (map(.output_tokens) | add),
        total_cache_creation_tokens: (map(.cache_creation_input_tokens // 0) | add),
        total_cache_read_tokens: (map(.cache_read_input_tokens // 0) | add),
        total_tokens: ((map(.input_tokens) | add) + (map(.output_tokens) | add)),
        avg_input_per_step: ((map(.input_tokens) | add) / length | floor),
        avg_output_per_step: ((map(.output_tokens) | add) / length | floor),
        cache_hit_rate: (if (map(.cache_read_input_tokens // 0) | add) > 0 then
            ((map(.cache_read_input_tokens // 0) | add) / ((map(.cache_creation_input_tokens // 0) | add) + (map(.cache_read_input_tokens // 0) | add)) * 100 | floor)
            else 0 end)
    }'
echo ""

# 7. User message
echo "=== User Message ==="
grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -r 'select(.event_type == "agent.run.start") | .payload.user_message'
echo ""

echo "=========================================="
echo "Analysis complete!"
echo "=========================================="

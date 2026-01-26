#!/bin/bash
# Verify Prompt Caching Implementation
#
# This script checks if prompt caching is working correctly by examining
# the most recent trace for cache usage.

set -e

LOG_FILE="$HOME/.mash/logs/codebase_v2.jsonl"

echo "=========================================="
echo "Prompt Caching Verification"
echo "=========================================="
echo ""

# Get the most recent trace
echo "Finding most recent trace..."
TRACE_ID=$(tail -100 "$LOG_FILE" | jq -r 'select(.event_type=="agent.run.start") | .trace_id' | tail -1)

if [ -z "$TRACE_ID" ]; then
    echo "❌ No recent traces found. Run the agent first."
    exit 1
fi

echo "✓ Found trace: $TRACE_ID"
echo ""

# Check if caching is enabled in the implementation
echo "=== Implementation Check ==="
if grep -q "use_prompt_caching" src/mash/core/llm.py; then
    echo "✓ Prompt caching code is present in llm.py"
else
    echo "❌ Prompt caching code NOT found in llm.py"
    exit 1
fi

if grep -q "prompt_caching_enabled" src/mash/core/config.py; then
    echo "✓ Configuration flag is present in config.py"
else
    echo "❌ Configuration flag NOT found in config.py"
    exit 1
fi

if grep -q "cache_creation_input_tokens" src/mash/logging/events.py; then
    echo "✓ Cache logging fields are present in events.py"
else
    echo "❌ Cache logging fields NOT found in events.py"
    exit 1
fi

echo ""

# Check cache usage in logs
echo "=== Cache Usage Analysis for $TRACE_ID ==="

# Get all LLM requests for this trace
REQUESTS=$(grep "\"trace_id\": \"$TRACE_ID\"" "$LOG_FILE" | \
    jq -s '[.[] | select(.event_type == "llm.request.complete")]')

TOTAL_REQUESTS=$(echo "$REQUESTS" | jq 'length')

if [ "$TOTAL_REQUESTS" -eq 0 ]; then
    echo "❌ No LLM requests found for this trace"
    exit 1
fi

echo "Total LLM requests: $TOTAL_REQUESTS"
echo ""

# Check first request for cache creation
echo "Step 1 (Cache Write):"
FIRST_REQUEST=$(echo "$REQUESTS" | jq '.[0]')
CACHE_CREATION=$(echo "$FIRST_REQUEST" | jq '.cache_creation_input_tokens // 0')
INPUT_TOKENS=$(echo "$FIRST_REQUEST" | jq '.input_tokens')

echo "  Input tokens: $INPUT_TOKENS"
echo "  Cache creation tokens: $CACHE_CREATION"

if [ "$CACHE_CREATION" -gt 0 ]; then
    echo "  ✓ Cache write detected!"
    CACHE_PCT=$(echo "scale=1; $CACHE_CREATION * 100 / $INPUT_TOKENS" | bc)
    echo "  ✓ Cached $CACHE_CREATION tokens (${CACHE_PCT}% of input)"
else
    echo "  ⚠️  No cache creation detected"
    echo "  This could mean:"
    echo "    - Caching is disabled in config"
    echo "    - System prompt is < 1,024 tokens"
    echo "    - Model doesn't support caching"
fi

echo ""

# Check subsequent requests for cache reads
if [ "$TOTAL_REQUESTS" -gt 1 ]; then
    echo "Steps 2-$TOTAL_REQUESTS (Cache Reads):"
    CACHE_READS=$(echo "$REQUESTS" | jq '[.[1:] | .[] | .cache_read_input_tokens // 0] | add')
    CACHE_HIT_COUNT=$(echo "$REQUESTS" | jq '[.[1:] | .[] | select(.cache_read_input_tokens > 0)] | length')

    echo "  Total cache read tokens: $CACHE_READS"
    echo "  Requests with cache hits: $CACHE_HIT_COUNT / $(($TOTAL_REQUESTS - 1))"

    if [ "$CACHE_READS" -gt 0 ]; then
        echo "  ✓ Cache reads detected!"
        CACHE_HIT_RATE=$(echo "scale=0; $CACHE_HIT_COUNT * 100 / ($TOTAL_REQUESTS - 1)" | bc)
        echo "  ✓ Cache hit rate: ${CACHE_HIT_RATE}%"

        # Show per-request cache reads
        echo ""
        echo "  Per-request breakdown:"
        echo "$REQUESTS" | jq -r '.[1:] | .[] |
            "    Step: \(.ts | tostring | split(".")[0]) → Cache read: \(.cache_read_input_tokens // 0) tokens"' | \
            head -5
    else
        echo "  ⚠️  No cache reads detected"
        echo "  This could mean:"
        echo "    - Requests were > 5 minutes apart (cache expired)"
        echo "    - System prompt or tools changed between requests"
        echo "    - Only 1 request was made"
    fi
else
    echo "⚠️  Only 1 request in this trace - can't verify cache reads"
    echo "   Cache reads happen on 2nd+ requests"
fi

echo ""

# Calculate savings
echo "=== Cache Savings Analysis ==="

TOTAL_INPUT=$(echo "$REQUESTS" | jq '[.[] | .input_tokens] | add')
TOTAL_CACHE_CREATION=$(echo "$REQUESTS" | jq '[.[] | .cache_creation_input_tokens // 0] | add')
TOTAL_CACHE_READS=$(echo "$REQUESTS" | jq '[.[] | .cache_read_input_tokens // 0] | add')

echo "Total input tokens: $TOTAL_INPUT"
echo "Total cache creation tokens: $TOTAL_CACHE_CREATION"
echo "Total cache read tokens: $TOTAL_CACHE_READS"
echo ""

if [ "$TOTAL_CACHE_CREATION" -gt 0 ] && [ "$TOTAL_CACHE_READS" -gt 0 ]; then
    # Calculate effective cost
    # Cache write: ×1.25, Cache read: ×0.1, Normal: ×1.0
    CACHE_WRITE_COST=$(echo "scale=2; $TOTAL_CACHE_CREATION * 1.25" | bc)
    CACHE_READ_COST=$(echo "scale=2; $TOTAL_CACHE_READS * 0.1" | bc)
    UNCACHED=$(echo "$TOTAL_INPUT - $TOTAL_CACHE_CREATION - $TOTAL_CACHE_READS" | bc)
    UNCACHED_COST=$UNCACHED

    EFFECTIVE_COST=$(echo "$CACHE_WRITE_COST + $CACHE_READ_COST + $UNCACHED_COST" | bc)

    echo "Cost calculation:"
    echo "  Cache writes: $TOTAL_CACHE_CREATION × 1.25 = $CACHE_WRITE_COST token-equiv"
    echo "  Cache reads: $TOTAL_CACHE_READS × 0.1 = $CACHE_READ_COST token-equiv"
    echo "  Uncached: $UNCACHED × 1.0 = $UNCACHED_COST token-equiv"
    echo "  ────────────────────────────────────"
    echo "  Effective cost: $EFFECTIVE_COST token-equiv"
    echo ""

    SAVINGS=$(echo "scale=0; ($TOTAL_INPUT - $EFFECTIVE_COST) * 100 / $TOTAL_INPUT" | bc)
    echo "✓ Cache savings: $SAVINGS% on this trace"
    echo "  (Without caching: $TOTAL_INPUT tokens)"
    echo "  (With caching: $EFFECTIVE_COST token-equiv)"
elif [ "$TOTAL_CACHE_CREATION" -gt 0 ]; then
    echo "⚠️  Only cache writes detected (no reads yet)"
    echo "   Run another query in the same session to see cache reads"
elif [ "$TOTAL_CACHE_READS" -gt 0 ]; then
    echo "⚠️  Only cache reads detected (no writes?)"
    echo "   This shouldn't happen - please check implementation"
else
    echo "❌ No caching detected"
    echo "   Caching may be disabled or not working"
fi

echo ""
echo "=========================================="
echo "Verification Complete!"
echo "=========================================="
echo ""

# Summary
if [ "$CACHE_CREATION" -gt 0 ] && [ "$TOTAL_CACHE_READS" -gt 0 ]; then
    echo "✅ PROMPT CACHING IS WORKING!"
    echo "   - Cache writes: ✓"
    echo "   - Cache reads: ✓"
    echo "   - Savings: ${SAVINGS}%"
elif [ "$CACHE_CREATION" -gt 0 ]; then
    echo "⚠️  PARTIAL SUCCESS"
    echo "   - Cache writes: ✓"
    echo "   - Cache reads: ✗ (need multi-step query)"
    echo "   Run a multi-step query to verify cache reads"
else
    echo "❌ PROMPT CACHING NOT DETECTED"
    echo "   Check configuration and implementation"
fi

echo ""
echo "To run full analysis:"
echo "  ./scripts/analyze_token_usage.sh $TRACE_ID"

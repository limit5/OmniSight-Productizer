#!/bin/sh
# OP-2499 - live-fleet smoke script (brain+HEAD JSON)
#
# Prints a single-line JSON object describing the runner brain
# and the current git HEAD short SHA:
#   {"brain":"grok","head":"<short-sha>"}
#
# 4-AC: Code(script+exec) / Deploy(any POSIX) / Integration(valid JSON) / Exercised(runs+prints).

set -eu

# Retrieve current git HEAD short SHA using git
head=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

# Clean git HEAD short SHA of any newlines or carriage returns
head=$(printf '%s' "$head" | tr -d '\n\r')

# Escape backslashes and double quotes defensively for JSON compatibility
escaped_head=$(printf '%s' "$head" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')

printf '{"brain":"grok","head":"%s"}\n' "$escaped_head"
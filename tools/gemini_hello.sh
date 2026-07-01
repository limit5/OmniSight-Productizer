#!/bin/sh
# OP-2497 - Gemini brain smoke test hello script.
#
# Prints a one-line JSON document:
#   {"brain":"gemini","ok":true,"host":"<hostname>"}
# using the running hostname.
#
# 4-AC: Code(script+exec) / Deploy(any POSIX) / Integration(valid JSON) / Exercised(runs+prints).

set -eu

# Retrieve hostname using portable POSIX commands
host=$(hostname 2>/dev/null || uname -n 2>/dev/null || echo "unknown")

# Clean hostname of any newlines or carriage returns
host=$(printf '%s' "$host" | tr -d '\n\r')

# Escape backslashes and double quotes defensively for JSON compatibility
escaped_host=$(printf '%s' "$host" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')

printf '{"brain":"gemini","ok":true,"host":"%s"}\n' "$escaped_host"

#!/bin/bash
# Run from a source checkout without installing
cd "$(dirname "$0")"
exec python3 src/main.py "$@"

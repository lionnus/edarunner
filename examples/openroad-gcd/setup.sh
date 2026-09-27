#!/usr/bin/env bash
# Make the source repository of the example from design/. Rerun is safe.
set -e
cd "$(dirname "$0")"
rm -rf repo && mkdir repo && cp design/config.mk repo/ && git -C repo init -q -b main && git -C repo add -A && git -C repo -c user.name=gcd -c user.email=gcd@example.com commit -q -m "gcd design"
echo "repo ready at $(pwd)/repo, HEAD $(git -C repo rev-parse --short HEAD)"

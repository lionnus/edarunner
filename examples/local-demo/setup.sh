#!/usr/bin/env bash
# Make the source repository of the demo from flow/, and the scratch that site.toml names. Rerun is safe.
set -e
cd "$(dirname "$0")"
rm -rf repo && mkdir repo && cp -r flow repo/ && git -C repo init -q -b main && git -C repo add -A && git -C repo -c user.name=demo -c user.email=demo@example.com commit -q -m "demo flow"
# Open to every user, as /tmp is, since the first user to run the demo on a host creates it.
mkdir -p -m 1777 /tmp/edr-demo
echo "scratch ready at /tmp/edr-demo"
echo "repo ready at $(pwd)/repo, HEAD $(git -C repo rev-parse --short HEAD)"

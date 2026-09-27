#!/usr/bin/env bash
# The seat probe of one FlexLM feature: flexlm_free.sh <port@server> <feature>
# Prints `free total` on one line, the shape a [tools.<name>] probe must print.
# Exit 1 when lmstat does not list the feature; the driver then lets the stage run.
set -uo pipefail
server=$1 feature=$2
lmutil lmstat -a -c "$server" -f "$feature" | awk -v f="$feature:" '
  $1 == "Users" && $3 == f { print $6 - $11, $6; found = 1; exit }
  END { exit !found }'

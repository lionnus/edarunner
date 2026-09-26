#!/usr/bin/env bash
# A fake FlexLM probe. DEMO_LIC_USED sets the seats in use (default 2 of 10).
printf 'Users of demo:  (Total of 10 licenses issued;  Total of %s licenses in use)\n' "${DEMO_LIC_USED:-2}"

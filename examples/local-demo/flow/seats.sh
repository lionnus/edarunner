#!/usr/bin/env bash
# A stand-in seat probe: prints `free total`. DEMO_SEATS_USED sets the seats in use (default 2 of 10).
echo "$((10 - ${DEMO_SEATS_USED:-2})) 10"

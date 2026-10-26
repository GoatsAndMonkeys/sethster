#!/bin/zsh
# Usage: tools/shot.sh <name> [route] [extra launch args...]  — relaunch app on the booted sim and screenshot.
cd "$(dirname $0)/.."
NAME=$1; ROUTE=$2; shift 2 2>/dev/null
ARGS=()
[[ -n "$ROUTE" ]] && ARGS=(-route "$ROUTE")
xcrun simctl launch --terminate-running-process booted com.goatsandmonkeys.sethster $ARGS $@ >/dev/null
sleep ${WAIT:-4}
xcrun simctl io booted screenshot screenshots/$NAME.png >/dev/null 2>&1 && echo screenshots/$NAME.png

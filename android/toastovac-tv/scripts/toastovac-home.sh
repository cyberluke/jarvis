#!/bin/sh
# Restore Toastovač HOME. Overlay stays off.
SERIAL="${1:-192.168.1.122:5555}"
adb connect "$SERIAL" >/dev/null
adb -s "$SERIAL" shell am stopservice -a ai.toastovac.tv.STOP_OVERLAY -n ai.toastovac.tv/.CompanionOverlayService
adb -s "$SERIAL" shell am start -n ai.toastovac.tv/.MainActivity
echo "Toastovač HOME started on $SERIAL (overlay not started)"

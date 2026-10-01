#!/bin/sh
# Rescue: stop Toastovač overlay, enable stock launcher, open HOME + Settings.
SERIAL="${1:-192.168.1.122:5555}"
adb connect "$SERIAL" >/dev/null
adb -s "$SERIAL" shell am stopservice -a ai.toastovac.tv.STOP_OVERLAY -n ai.toastovac.tv/.CompanionOverlayService
adb -s "$SERIAL" shell am stopservice -n ai.toastovac.tv/.CompanionOverlayService
adb -s "$SERIAL" shell am stopservice -n ai.toastovac.tv/.ProbeOverlayService
adb -s "$SERIAL" shell pm enable com.google.android.tvlauncher
adb -s "$SERIAL" shell am force-stop ai.toastovac.tv
adb -s "$SERIAL" shell am start -n com.google.android.tvlauncher/.MainActivity
sleep 2
adb -s "$SERIAL" shell am start -n com.android.tv.settings/.device.displaysound.DisplaySoundActivity
echo "stock HOME + Display & Sound started on $SERIAL"
echo "restore later: android/toastovac-tv/scripts/toastovac-home.sh"

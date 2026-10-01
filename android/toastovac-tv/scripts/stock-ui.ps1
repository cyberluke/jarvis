# Rescue: stop Toastovač overlay, enable stock launcher, open HOME + Settings.
# Does not uninstall Toastovač. Does not wipe state.
param(
    [string]$Serial = "192.168.1.122:5555"
)
$ErrorActionPreference = "Continue"
adb connect $Serial | Out-Null
adb -s $Serial shell "am stopservice -a ai.toastovac.tv.STOP_OVERLAY -n ai.toastovac.tv/.CompanionOverlayService"
adb -s $Serial shell "am stopservice -n ai.toastovac.tv/.CompanionOverlayService"
adb -s $Serial shell "am stopservice -n ai.toastovac.tv/.ProbeOverlayService"
adb -s $Serial shell "pm enable com.google.android.tvlauncher"
adb -s $Serial shell "am force-stop ai.toastovac.tv"
adb -s $Serial shell "am start -n com.google.android.tvlauncher/.MainActivity"
Start-Sleep -Seconds 2
adb -s $Serial shell "am start -n com.android.tv.settings/.device.displaysound.DisplaySoundActivity"
Write-Output "stock HOME + Display & Sound started on $Serial"
Write-Output "restore later: powershell -File android\toastovac-tv\scripts\toastovac-home.ps1"

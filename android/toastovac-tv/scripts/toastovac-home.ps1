# Restore Toastovač as the live HOME dashboard. Overlay stays off.
param(
    [string]$Serial = "192.168.1.122:5555"
)
$ErrorActionPreference = "Continue"
adb connect $Serial | Out-Null
adb -s $Serial shell "am stopservice -a ai.toastovac.tv.STOP_OVERLAY -n ai.toastovac.tv/.CompanionOverlayService"
adb -s $Serial shell "am start -n ai.toastovac.tv/.MainActivity"
Write-Output "Toastovač HOME started on $Serial (overlay not started)"

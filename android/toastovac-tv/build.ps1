# Manual APK build (no gradle): aapt2 -> javac -> d8 -> jar -> zipalign -> apksigner.
# Requires: JDK on PATH, Android SDK at $sdk (platforms;android-37.2, build-tools;37.0.0).
$ErrorActionPreference = 'Stop'

$sdk  = 'C:\Users\lukes.COREI9\AppData\Local\Android\Sdk'
$bt   = "$sdk\build-tools\37.0.0"
$plat = "$sdk\platforms\android-37.2"
$jdk  = 'C:\Program Files\Java\jdk-24'
$root = $PSScriptRoot
$out  = "$root\out"

foreach ($p in @($bt, $plat)) { if (!(Test-Path $p)) { throw "missing: $p" } }

# ── clean ──
Remove-Item "$out" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$out\res", "$out\classes", "$out\dex", "$out\gen" | Out-Null

# ── resources ──
Write-Host "== aapt2 compile/link =="
& "$bt\aapt2.exe" compile --dir "$root\res" -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }
& "$bt\aapt2.exe" link -o "$out\app.unsigned.apk" -I "$plat\android.jar" --manifest "$root\AndroidManifest.xml" -R "$out\res.zip" --java "$out\gen" --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

# ── java ──
Write-Host "== javac =="
$srcs = Get-ChildItem "$root\src" -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& javac --release 11 -classpath "$plat\android.jar" -d "$out\classes" $srcs
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

# ── native (JNI) — NDK, 32-bit only (box is armeabi-v7a) ──
Write-Host "== JNI =="
$ndkRoot = 'C:\Users\lukes.COREI9\AppData\Local\Android\Sdk\ndk\27.2.12479018'
$clang = "$ndkRoot\toolchains\llvm\prebuilt\windows-x86_64\bin\armv7a-linux-androideabi21-clang.cmd"
$jniOut = "$root\out\jni"
New-Item -ItemType Directory -Force -Path "$jniOut\lib\armeabi-v7a" | Out-Null
if (Test-Path "$root\jni") {
    $csrc = Get-ChildItem "$root\jni" -Filter *.c | ForEach-Object { $_.FullName }
    if ($csrc) {
        & $clang -shared -fPIC -O2 -Wall -o "$jniOut\lib\armeabi-v7a\libyuvplane.so" $csrc -landroid -llog
        if ($LASTEXITCODE -ne 0) { throw "clang failed" }
    }
}

# ── dex ──
Write-Host "== d8 =="
$classes = Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName }
& "$bt\d8.bat" --release --lib "$plat\android.jar" --min-api 26 --output "$out\dex" $classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

# ── package dex + assets + native libs into apk ──
Write-Host "== package =="
& "$jdk\bin\jar.exe" uf "$out\app.unsigned.apk" -C "$out\dex" classes.dex
if ($LASTEXITCODE -ne 0) { throw "jar dex failed" }
if (Test-Path "$jniOut\lib") {
    & "$jdk\bin\jar.exe" uf "$out\app.unsigned.apk" -C "$jniOut" lib
    if ($LASTEXITCODE -ne 0) { throw "jar jni failed" }
}
Get-ChildItem "$root\assets" -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring("$root\assets\".Length)
    & "$jdk\bin\jar.exe" uf "$out\app.unsigned.apk" -C "$root\assets" $rel
    if ($LASTEXITCODE -ne 0) { throw "jar assets failed: $rel" }
}

# ── align ──
Write-Host "== zipalign =="
& "$bt\zipalign.exe" -f 4 "$out\app.unsigned.apk" "$out\app.aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

# ── sign (create keystore on first build) ──
Write-Host "== apksigner =="
$ks = "$root\keys\toastovac.keystore"
if (!(Test-Path $ks)) {
    New-Item -ItemType Directory -Force -Path "$root\keys" | Out-Null
    & "$jdk\bin\keytool.exe" -genkeypair -keystore $ks -alias toastovac -keyalg RSA -keysize 2048 -validity 10000 -storepass toastovac -keypass toastovac -dname "CN=Toastovac TV,O=Toastovac,C=CZ"
    if ($LASTEXITCODE -ne 0) { throw "keytool failed" }
}
& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:toastovac --key-pass pass:toastovac --out "$root\toastovac-tv.apk" "$out\app.aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }

$apk = "$root\toastovac-tv.apk"
Write-Host "OK: $apk ($([math]::Round((Get-Item $apk).Length/1KB)) KB)"
Write-Host "Deploy: adb -s 192.168.1.122:5555 install -r `"$apk`""
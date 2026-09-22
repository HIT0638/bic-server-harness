param([ValidateSet('Start', 'Stop')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'This provisioner is only for disposable GitHub-hosted Windows CI.'
}
$statePath = Join-Path $env:RUNNER_TEMP 'sshbridge-wsl-target.json'
if ($Action -eq 'Stop') {
    if (Test-Path $statePath) {
        $state = Get-Content $statePath -Raw | ConvertFrom-Json
        if ($state.run -ne $env:GITHUB_RUN_ID -or $state.name -notmatch '^sshbridge-ci-[0-9a-f]{32}$') {
            throw 'Refusing to remove an unrelated WSL distribution.'
        }
        $installed = (& wsl.exe --list --quiet) -replace "`0", ''
        if ($LASTEXITCODE -ne 0) { throw 'Cannot enumerate WSL distributions for cleanup.' }
        if (@($installed | ForEach-Object { $_.Trim() }) -contains $state.name) {
            & wsl.exe --unregister $state.name
            if ($LASTEXITCODE -ne 0) { throw 'Failed to remove the temporary WSL target.' }
        }
        Remove-Item $statePath
    }
    exit 0
}
if (Test-Path $statePath) { throw 'A test target already exists; run Stop first.' }
$name = 'sshbridge-ci-' + [Guid]::NewGuid().ToString('N')
$directory = Join-Path $env:RUNNER_TEMP $name
New-Item -ItemType Directory -Path $directory | Out-Null
@{ name = $name; run = $env:GITHUB_RUN_ID } | ConvertTo-Json | Set-Content $statePath
$base = 'https://cloud-images.ubuntu.com/wsl/jammy/current/'
$imageName = 'ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz'
$image = Join-Path $directory 'rootfs.tar.gz'
Invoke-WebRequest ($base + $imageName) -OutFile $image
$sumsFile = Join-Path $directory 'SHA256SUMS'
Invoke-WebRequest ($base + 'SHA256SUMS') -OutFile $sumsFile
$sums = Get-Content $sumsFile -Raw
$line = ($sums -split "`n") | Where-Object { $_ -match ([Regex]::Escape($imageName) + '$') }
if (@($line).Count -ne 1) { throw 'No unique official image checksum found.' }
$expected = ($line -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash $image -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expected -ne $actual) { throw 'Ubuntu rootfs checksum mismatch.' }
& wsl.exe --import $name (Join-Path $directory 'distro') $image --version 1
if ($LASTEXITCODE -ne 0) { throw 'WSL1 import failed.' }
$bootstrap = @'
set -eu
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends openssh-server
useradd --create-home --shell /bin/sh sshbridge-test
passwd -d sshbridge-test
mkdir -p /run/sshd
'@
& wsl.exe -d $name -u root --exec /bin/sh -c ($bootstrap -replace "`r", '')
if ($LASTEXITCODE -ne 0) { throw 'Linux SSH test target provisioning failed.' }
"SSHBRIDGE_WSL_DISTRO=$name" | Add-Content $env:GITHUB_ENV
$output = '.test-runtime/windows-linux'
New-Item -ItemType Directory -Force -Path $output | Out-Null
@{ distribution = 'Ubuntu 22.04'; mode = 'WSL1'; image_sha256 = $actual; image_url = $base + $imageName } |
    ConvertTo-Json | Set-Content (Join-Path $output 'target.json')

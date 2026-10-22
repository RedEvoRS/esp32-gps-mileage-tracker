# Piece 2: run on the Windows computer connected to the Supreme.
function Invoke-TrackerFlash {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive host UI must stay separate from function return values and work with irm | iex.')]
    [CmdletBinding()]
    param(
        [string]$Server, [string]$InstallDir = '~/mileage-tracker',
        [switch]$Cleanup, [switch]$FirstInstall, [string]$Port, [string]$Path,
        [switch]$DryRun, [switch]$Help, [string]$Esptool, [string]$BackupDir, [switch]$SkipBackup
    )
    $ErrorActionPreference = 'Stop'
    $releaseTag = '@RELEASE_TAG@'
    $esptoolVersion = '5.6.0'
    # Published release asset digest (windows-amd64.zip):
    # https://api.github.com/repos/espressif/esptool/releases/tags/v5.6.0
    # https://github.com/espressif/esptool/releases/tag/v5.6.0
    $esptoolHash = 'd55a96fbeb97fd4cb05bd93f201e5c8f6518b7bfcde2fc6966fd0190fd891629'

    # BEGIN PURE HELPERS (also exercised by install/tests/flash.Tests.ps1)
    function Get-FlashPart {
        param($Manifest, [bool]$First)
        foreach ($name in @('bootloader', 'partitions', 'boot_app0', 'app')) {
            if ($First -or $name -eq 'app') { $Manifest.parts | Where-Object { $_.name -ceq $name } }
        }
    }
    function Test-FlashManifest {
        param([string]$Folder, [bool]$IsCleanup)
        $manifestPath = Join-Path $Folder 'manifest.json'
        if ((Get-Item -LiteralPath $manifestPath).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Manifest symlink is not allowed.' }
        $m = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if (($m.schema -isnot [int] -and $m.schema -isnot [long]) -or $m.schema -ne 1 -or $m.chip -cne 'esp32s3' -or
            $m.kind -cnotin @('enroll', 'normal') -or ($IsCleanup -and $m.kind -cne 'normal') -or
            $m.version -isnot [string] -or $m.commit -cnotmatch '^[0-9a-fA-F]{40}$' -or
            $m.fqbn -cnotlike 'esp32:esp32:esp32s3:*' -or
            $m.flash_mode -cnotin @('dio', 'qio', 'dout', 'qout') -or
            $m.flash_freq -cnotin @('80m', '40m', '20m') -or $m.flash_size -cne '8MB' -or
            $m.app_offset -cnotmatch '^0x[0-9a-fA-F]{1,8}$' -or
            $m.parts -isnot [array] -or $m.parts.Count -ne 4) { throw 'Invalid firmware manifest metadata.' }
        $names = @($m.parts | ForEach-Object { $_.name } | Sort-Object)
        if (($names -join ',') -cne 'app,boot_app0,bootloader,partitions') { throw 'Expected exactly four named firmware parts.' }
        [long]$previous = 0
        foreach ($part in @(Get-FlashPart $m $true)) {
            if ($part.file -cne ($part.name + '.bin') -or
                $part.offset -cnotmatch '^0x[0-9a-fA-F]{1,8}$' -or
                $part.sha256 -cnotmatch '^[0-9a-fA-F]{64}$') { throw 'Invalid firmware part.' }
            $file = Join-Path $Folder $part.file
            $item = Get-Item -LiteralPath $file
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Firmware must be a regular file.' }
            if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $part.sha256) { throw ('Firmware checksum mismatch: ' + $part.name) }
            [long]$start = [Convert]::ToInt64($part.offset.Substring(2), 16)
            [long]$end = $start + $item.Length
            if ($item.Length -le 0 -or $start % 4096 -ne 0 -or $start -lt $previous) { throw 'Empty, overlapping or unaligned firmware part.' }
            # Safety fences from the Supreme sketch partitions.csv. Commands
            # use manifest offsets, never these fence values.
            $safe = switch -CaseSensitive ($part.name) {
                'bootloader' { $end -le 0x8000 }
                'partitions' { $start -ge 0x8000 -and $end -le 0x9000 }
                'boot_app0' { $start -ge 0xe000 -and $end -le 0x10000 }
                'app' { $start -ge 0x10000 -and $end -le 0x190000 -and $part.offset -ceq $m.app_offset }
            }
            if (-not $safe) { throw 'Part crosses a reserved region (NVS or trip storage), or app_offset differs.' }
            $previous = $end
        }
        return $m
    }
    function Select-FlashPort {
        param([string]$Requested, [string[]]$Candidates, [string]$Choice)
        if ($Requested) { return $Requested }
        if ($Candidates.Count -eq 1) { return $Candidates[0] }
        if ($Choice -notmatch '^[1-9][0-9]*$' -or [long]$Choice -gt $Candidates.Count) { throw 'Invalid port choice; use -Port or choose a listed number.' }
        return $Candidates[[int]$Choice - 1]
    }
    # END PURE HELPERS

    function New-PrivateFolder {
        [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Internal temporary-folder creation is required even in DryRun to validate private firmware.')]
        [CmdletBinding()]
        param([string]$Parent)
        # Pass the ACL at creation on .NET Framework. On .NET (PS7), use the
        # equivalent FileSystemAclExtensions API; no permissive creation gap.
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.SetOwner($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
        $directory = Join-Path $Parent ('tracker-flash-' + [guid]::NewGuid().ToString('N'))
        if ($PSVersionTable.PSEdition -eq 'Core') {
            [IO.FileSystemAclExtensions]::CreateDirectory($acl, $directory) | Out-Null
        } else { [IO.Directory]::CreateDirectory($directory, $acl) | Out-Null }
        return $directory
    }
    function Invoke-FlashCommand {
        param([string[]]$Arguments)
        if ($DryRun) {
            $quoted = @($Esptool) + $Arguments | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }
            Write-Host ('DRY RUN: & ' + ($quoted -join ' '))
        } else {
            & $Esptool @Arguments
            if ($LASTEXITCODE -ne 0) { throw 'esptool failed. Close serial monitors, check the port and re-enter download mode.' }
        }
    }
    function Read-FactoryRange {
        param([long]$Offset, [int]$Length, [string]$Destination)
        # Windows native USB can fail repeatedly on a particular 4 KB block.
        # Recursively split failed ranges down to 2 KB, then concatenate them.
        $arguments = $common + @($readCommand, ('0x{0:x}' -f $Offset), ('0x{0:x}' -f $Length), $Destination)
        $savedPreference = $ErrorActionPreference
        try {
            # Native stderr must not abort recovery under Windows PowerShell.
            $ErrorActionPreference = 'Continue'
            & $Esptool @arguments
            $readExitCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $savedPreference }
        if ($readExitCode -eq 0 -and (Test-Path -LiteralPath $Destination) -and
            (Get-Item -LiteralPath $Destination).Length -eq $Length) { return }
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        if ($Length -le 2048) { throw 'Factory read failed even at 2 KB. No write performed; check USB cable and download mode.' }
        $half = [int]($Length / 2)
        $left = Join-Path $temp ([guid]::NewGuid().ToString('N') + '.bin')
        $right = Join-Path $temp ([guid]::NewGuid().ToString('N') + '.bin')
        Read-FactoryRange $Offset $half $left
        Read-FactoryRange ($Offset + $half) ($Length - $half) $right
        $output = [IO.File]::Create($Destination)
        try {
            foreach ($piece in @($left, $right)) {
                $inputFile = [IO.File]::OpenRead($piece)
                try { $inputFile.CopyTo($output) } finally { $inputFile.Dispose() }
            }
        } finally { $output.Dispose() }
        Remove-Item -LiteralPath $left, $right -Force
    }
    if ($Help) {
        Write-Host @'
Usage: .\flash.ps1 [-Server <user>@<host>] [-InstallDir ~/mileage-tracker]
  [-Path <folder>] [-Cleanup] [-FirstInstall] [-Port COMx] [-DryRun]
  [-Esptool <executable>] [-BackupDir <folder>] [-SkipBackup] [-Help]
Windows PowerShell 5.1 / PowerShell 7; Windows 10/11 native USB needs no driver.
Requires OpenSSH scp unless -Path is used. Standalone esptool needs no Python.
One-liner: irm <release-url>/flash.ps1 | iex (prompts use Read-Host).
Download, read, run alternative:
  Invoke-WebRequest <release-url>/flash.ps1 -OutFile flash.ps1
  Get-Content .\flash.ps1
  powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server <user>@<host>
PowerShell 7: use pwsh instead of powershell.exe.
-DryRun verifies firmware and esptool but never opens a serial device.
Default update and -Cleanup write only the app and never erase settings or trips.
-FirstInstall: verified double 8 MB backup, type ERASE, erase all flash, write four parts.
Use only for a new/factory board. Internal settings and trips are wiped; SD is untouched.
-FirstInstall -SkipBackup: re-provision a board you already backed up; still type ERASE.
Dry run prints the backup/erase/write plan without requesting confirmation.
The standalone esptool version is pinned independently of the tracker release.
'@
        return
    }
    $temp = $null; $toolTemp = $null
    try {
        if ($Cleanup -and $FirstInstall) { throw 'Cleanup is app-only; cannot combine with FirstInstall.' }
        if ($SkipBackup -and -not $FirstInstall) { throw 'SkipBackup requires explicit FirstInstall.' }
        $temp = New-PrivateFolder $env:TEMP
        $payload = Join-Path $temp 'payload'
        if ($Path) {
            [IO.Directory]::CreateDirectory($payload) | Out-Null
            foreach ($file in @('manifest.json', 'bootloader.bin', 'partitions.bin', 'boot_app0.bin', 'app.bin')) {
                $source = Join-Path $Path $file
                if ((Get-Item -LiteralPath $source).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Firmware symlinks are not allowed.' }
                # Write fresh files so an input file's permissive ACL is not copied.
                [IO.File]::WriteAllBytes((Join-Path $payload $file), [IO.File]::ReadAllBytes($source))
            }
        } else {
            if (-not $Server) { $Server = Read-Host 'SSH server (<user>@<host>)' }
            if ($Server -cnotmatch '^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+$') { throw 'Use an SSH server in user@host form.' }
            if ($InstallDir -cnotmatch '^(~/|/)[a-zA-Z0-9_./-]+$' -or $InstallDir.Contains('/../')) { throw 'Install directory must be a simple absolute or ~/ path.' }
            $kind = 'current'; if ($Cleanup) { $kind = 'cleanup' }
            & scp -rq "${Server}:$($InstallDir.TrimEnd('/'))/firmware-out/$kind/" $payload
            if ($LASTEXITCODE -ne 0) { throw 'Firmware copy failed.' }
        }
        try { $manifest = Test-FlashManifest $payload ([bool]$Cleanup) }
        catch { throw 'Invalid firmware manifest, unsafe offsets, kind, size or checksum.' }
        if (-not $Esptool) {
            if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
                throw 'The pinned Windows standalone release is x64. Supply -Esptool for another architecture.'
            }
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $toolTemp = New-PrivateFolder ([Environment]::GetFolderPath('UserProfile'))
            $archive = Join-Path $toolTemp 'esptool.zip'
            Write-Host "Downloading esptool $esptoolVersion from Espressif GitHub releases (third-party host)."
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "https://github.com/espressif/esptool/releases/download/v$esptoolVersion/esptool-v$esptoolVersion-windows-amd64.zip" -OutFile $archive
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine $esptoolHash) { throw 'esptool archive checksum mismatch.' }
            Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $toolTemp 'tool')
            $Esptool = (Get-ChildItem -LiteralPath $toolTemp -Filter esptool.exe -Recurse | Select-Object -First 1).FullName
            if (-not $Esptool) { throw 'esptool executable missing from archive.' }
        }
        $versionOutput = (& $Esptool version 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch 'v?([45])\.[0-9]+') { throw 'A working standalone esptool v4 or v5 executable is required.' }
        $major = $Matches[1]
        $before = 'no-reset'; $after = 'no-reset'; $hardReset = 'hard-reset'; $chipCommand = 'chip-id'; $readCommand = 'read-flash'; $writeCommand = 'write-flash'
        $modeFlag = '--flash-mode'; $freqFlag = '--flash-freq'; $sizeFlag = '--flash-size'
        $eraseCommand = 'erase-flash'
        if ($major -eq '4') {
            $before = 'no_reset'; $after = 'no_reset'; $hardReset = 'hard_reset'; $chipCommand = 'chip_id'; $readCommand = 'read_flash'; $writeCommand = 'write_flash'
            $modeFlag = '--flash_mode'; $freqFlag = '--flash_freq'; $sizeFlag = '--flash_size'
            $eraseCommand = 'erase_flash'
        }
        Write-Host "Tracker flash (release $releaseTag). Keep the LoRa antenna attached; use a USB data cable."
        Write-Host 'Download mode: hold BOOT, plug in USB (or tap RESET), wait 2-3 seconds, release BOOT.'
        Write-Host 'Close serial monitors. Native USB needs no Windows driver; COM number can change in download mode.'
        if (-not $DryRun) { $null = Read-Host 'Enter download mode, then press Enter' }
        $candidates = @([IO.Ports.SerialPort]::GetPortNames() | Sort-Object)
        try {
            $preferred = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPDeviceID -match 'VID_303A' -and $_.Name -match '\(COM[0-9]+\)' } |
                ForEach-Object { if ($_.Name -match '\((COM[0-9]+)\)') { $Matches[1] } } | Sort-Object -Unique)
            $candidates = @($preferred) + @($candidates | Where-Object { $_ -notin $preferred })
        } catch { Write-Verbose 'PnP metadata unavailable; using serial port names.' }
        $choice = ''
        if (-not $Port) {
            if ($candidates.Count -eq 0) { throw 'No candidate ports. Re-enter download mode or supply -Port.' }
            for ($i = 0; $i -lt $candidates.Count; $i++) { Write-Host ("{0}) {1}" -f ($i + 1), $candidates[$i]) }
            if ($candidates.Count -gt 1) { $choice = Read-Host 'Choose port number (Espressif ports listed first)' }
        }
        $Port = Select-FlashPort $Port $candidates $choice
        $common = @('--chip', 'esp32s3', '--port', $Port, '--before', $before, '--after', $after)
        Invoke-FlashCommand ($common + @($chipCommand))
        if (-not $FirstInstall -and -not $Cleanup -and -not $DryRun) {
            $FirstInstall = (Read-Host 'First install on a factory board? Backup, full erase, then write four parts? [y/N]') -match '^(y|yes)$'
        }
        if ($FirstInstall) {
            if (-not $SkipBackup) {
                if (-not $BackupDir) {
                    $BackupDir = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'mileage-tracker-backups'
                    if (-not $DryRun) { $answer = Read-Host "Backup folder [$BackupDir]"; if ($answer) { $BackupDir = $answer } }
                }
                # Use a private child folder even inside a user-chosen/shared parent.
                if (-not $DryRun) {
                    [IO.Directory]::CreateDirectory($BackupDir) | Out-Null
                    $BackupDir = New-PrivateFolder $BackupDir
                }
                $backup = Join-Path $BackupDir ('supreme-factory-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.bin')
                $second = $backup + '.verify.bin'
                if ($DryRun) {
                    Invoke-FlashCommand ($common + @($readCommand, '0', '0x800000', $backup))
                    Invoke-FlashCommand ($common + @($readCommand, '0', '0x800000', $second))
                } else {
                    Read-FactoryRange 0 8388608 $backup
                    Read-FactoryRange 0 8388608 $second
                    if ((Get-Item -LiteralPath $backup).Length -ne 8388608 -or (Get-Item -LiteralPath $second).Length -ne 8388608) { throw 'Factory backup is not 8 MB; no write performed.' }
                    if ((Get-FileHash -LiteralPath $backup).Hash -cne (Get-FileHash -LiteralPath $second).Hash) { throw 'Factory backup reads differ; no write performed. Keep both for investigation.' }
                    Remove-Item -LiteralPath $second -Force
                }
                Write-Host "Keep the factory backup privately outside the repo: $backup (may contain device keys)."
            } else { Write-Host 'Skipping factory backup: re-provision only a board you already backed up.' }
            Write-Host 'WARNING: This wipes everything on the board internal flash, including settings (trip counter, diagnostics enrollment record) and any trips on internal flash.'
            Write-Host 'This is right for a brand-new board or one coming from factory firmware. It is wrong for a tracker already running this project; use the default update instead.'
            Write-Host 'Trips on the SD card are not touched.'
            if ($DryRun) { Write-Host 'DRY RUN: A real first install requires typing ERASE. Backup verification is skipped only with explicit SkipBackup.' }
            elseif ((Read-Host 'Type ERASE to erase the whole internal flash') -cne 'ERASE') { throw 'Erase confirmation refused; nothing erased or written.' }
            Invoke-FlashCommand ($common + @($eraseCommand))
            Write-Host 'First install writes four separate parts after the full erase.'
        } else { Write-Host 'App-only update: settings, NVS trip counter, diag/enroll, LittleFS trips and SD trips are kept.' }
        $writeCommon = @($common); $writeCommon[-1] = $hardReset
        $writeArgs = $writeCommon + @($writeCommand, $modeFlag, $manifest.flash_mode, $freqFlag, $manifest.flash_freq, $sizeFlag, $manifest.flash_size)
        foreach ($part in @(Get-FlashPart $manifest ([bool]$FirstInstall))) { $writeArgs += @($part.offset, (Join-Path $payload $part.file)) }
        Invoke-FlashCommand $writeArgs
        Write-Host 'Press RESET once with BOOT released (hard reset) to run the sketch.'
        if ($FirstInstall) {
            Write-Host 'Expect on the serial console: LittleFS partition is fully erased; provisioning trip storage.'
            Write-Host 'Then: Setup complete. Logging trips.'
        }
        Write-Host 'Check the boot status card, GPS outdoors, WiFi uploads, and Device health.'
        if ($manifest.kind -ceq 'enroll') { Write-Host 'Wait for the first snapshot in Device health, then run this script again with -Cleanup (app only).' }
    } finally {
        # Firmware embeds WiFi credentials and the upload token. Always remove
        # our private copy, including after validation, download or flash failure.
        if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp -Recurse -Force }
        if ($toolTemp -and (Test-Path -LiteralPath $toolTemp)) { Remove-Item -LiteralPath $toolTemp -Recurse -Force }
    }
}
Invoke-TrackerFlash @args

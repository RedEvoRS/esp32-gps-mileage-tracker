# Plain PowerShell tests; no Pester/module dependency and no serial access.
# Run: powershell -ExecutionPolicy Bypass -File install/tests/flash.Tests.ps1
# Or:  pwsh -File install/tests/flash.Tests.ps1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', '', Justification = 'The dynamically loaded backup reader uses temp, common and readCommand from this scope.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Tests create only disposable fixtures in their own temporary directory.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidGlobalVars', '', Justification = 'The prompt queue crosses invoked script scopes through a Read-Host test double and is removed in finally.')]
param()
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'flash.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw
$helpers = ($source -split '# BEGIN PURE HELPERS[^\r\n]*\r?\n', 2)[1] -split '# END PURE HELPERS', 2
. ([scriptblock]::Create($helpers[0]))
$folder = Join-Path ([IO.Path]::GetTempPath()) ('flash-tests-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($folder) | Out-Null
$script:checks = 0
# The partition-table address is the ESP32-S3 default, not a CSV partition.
$partitionTableOffset = '0x8000'
$regions = @{}
$csv = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'firmware/04_trip_logger/partitions.csv'
Get-Content -LiteralPath $csv | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } | ForEach-Object {
    $cells = $_.Split(',') | ForEach-Object { $_.Trim() }
    $regions[$cells[0]] = @{ Offset = [Convert]::ToInt64($cells[3].Substring(2), 16); Size = [Convert]::ToInt64($cells[4].Substring(2), 16) }
}
$appStart = '0x{0:x}' -f $regions.factory.Offset
$appEnd = '0x{0:x}' -f ($regions.factory.Offset + $regions.factory.Size)
$nvsStart = '0x{0:x}' -f $regions.nvs.Offset

function Assert-Equal($Actual, $Expected) {
    if ($Actual -cne $Expected) { throw ("Expected '$Expected'; got '$Actual' at test line " + (Get-PSCallStack)[1].ScriptLineNumber) }
    $script:checks++
}
function Assert-Rejected([scriptblock]$Code) {
    $rejected = $false
    try { $null = & $Code } catch { $rejected = $true }
    Assert-Equal $rejected $true
}
function New-Fixture {
    $parts = @()
    foreach ($pair in @(@('bootloader', '0x0'), @('partitions', $partitionTableOffset), @('boot_app0', ('0x{0:x}' -f $regions.otadata.Offset)), @('app', $appStart))) {
        $file = Join-Path $folder ($pair[0] + '.bin')
        [IO.File]::WriteAllBytes($file, [Text.Encoding]::ASCII.GetBytes($pair[0] + ' synthetic test bytes'))
        $parts += @{ name = $pair[0]; offset = $pair[1]; file = $pair[0] + '.bin'; sha256 = (Get-FileHash $file).Hash }
    }
    return [pscustomobject]@{
        schema = 1; chip = 'esp32s3'; kind = 'enroll'; version = 'v0.08+0123456789'; commit = '0' * 40
        fqbn = 'esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled'
        flash_mode = 'dio'; flash_freq = '80m'; flash_size = '8MB'; app_offset = $appStart; parts = $parts
    }
}
function Save-Fixture($Manifest) {
    $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $folder 'manifest.json') -Encoding ASCII
}
try {
    foreach ($target in @('alias', 'alias-1.example', '_alias', 'user@host', 'user.name-1@127.0.0.1')) {
        Assert-Equal (Test-FlashServer $target) $true
    }
    foreach ($target in @('', '-alias', '-user@host', 'user@-host', 'user@@host', '@host', 'user@',
        ' alias', 'alias ', "alias`n", "alias`r", "alias`t", 'user host', 'host:22', '[::1]',
        'alias/path', 'alias\path', '$(id)', '`id`', 'alias;id', 'alias&', 'alias|id',
        'alias*', 'alias?', 'alias"', "alias'", 'alias%value', 'alias=value')) {
        Assert-Equal (Test-FlashServer $target) $false
    }
    Assert-Equal (Get-FlashLabel ('@' + 'RELEASE_TAG' + '@')) 'development checkout'
    Assert-Equal (Get-FlashLabel 'v0.00-test') 'release v0.00-test'
    $savedSystemRoot = $env:SystemRoot
    try {
        $env:SystemRoot = $folder
        Assert-Equal (Get-FlashScp) 'scp'
        $openSsh = Join-Path $folder 'System32\OpenSSH'
        [IO.Directory]::CreateDirectory($openSsh) | Out-Null
        $nativeScp = Join-Path $openSsh 'scp.exe'
        [IO.File]::WriteAllText($nativeScp, '')
        Assert-Equal (Get-FlashScp) $nativeScp
    } finally { $env:SystemRoot = $savedSystemRoot }
    $m = New-Fixture
    Save-Fixture $m
    $validated = Test-FlashManifest $folder $false
    Assert-Equal $validated.kind 'enroll'
    Assert-Equal ((Get-FlashPart $validated $true).name -join ',') 'bootloader,partitions,boot_app0,app'
    Assert-Equal ((Get-FlashPart $validated $false).name -join ',') 'app'
    Assert-Rejected { Test-FlashManifest $folder $true }
    $m.kind = 'normal'; Save-Fixture $m
    Assert-Equal (Test-FlashManifest $folder $true).kind 'normal'
    Assert-Equal ((Get-FlashPart $m $false).name -join ',') 'app'
    foreach ($change in @(@('schema', 2), @('schema', '1'), @('chip', 'esp32'), @('kind', 'other'),
        @('flash_size', '16MB'), @('flash_mode', 'bad'), @('flash_freq', 'bad'),
        @('commit', 'bad'), @('fqbn', 'other'), @('app_offset', '0x20000'))) {
        $m = New-Fixture; $m.($change[0]) = $change[1]; Save-Fixture $m
        Assert-Rejected { Test-FlashManifest $folder $false }
    }
    foreach ($change in @(@('file', '../app.bin'), @('sha256', ('0' * 64)), @('offset', $nvsStart),
        @('offset', $appEnd), @('offset', ('0x{0:x}' -f ($regions.factory.Offset + 1))), @('offset', '0x1;id'), @('name', 'bootloader'))) {
        $m = New-Fixture; $m.parts[3][$change[0]] = $change[1]; Save-Fixture $m
        Assert-Rejected { Test-FlashManifest $folder $false }
    }
    $m = New-Fixture; Save-Fixture $m
    [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]@(1, 2, 3))
    Assert-Rejected { Test-FlashManifest $folder $false }
    Remove-Item (Join-Path $folder 'app.bin')
    Assert-Rejected { Test-FlashManifest $folder $false }
    $m = New-Fixture
    [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]::new($regions.factory.Size + 1))
    $m.parts[3].sha256 = (Get-FileHash (Join-Path $folder 'app.bin')).Hash; Save-Fixture $m
    Assert-Rejected { Test-FlashManifest $folder $false }
    $m = New-Fixture; $m.parts[2].offset = $nvsStart; Save-Fixture $m
    Assert-Rejected { Test-FlashManifest $folder $false }
    Assert-Equal (Select-FlashPort 'COMx' @() '') 'COMx'
    Assert-Equal (Select-FlashPort '' @('COMx') '') 'COMx'
    Assert-Equal (Select-FlashPort '' @('COMx', 'COMy') '2') 'COMy'
    Assert-Rejected { Select-FlashPort '' @() '' }
    Assert-Rejected { Select-FlashPort '' @('COMx', 'COMy') '0' }
    Assert-Rejected { Select-FlashPort '' @('COMx', 'COMy') '3' }
    Assert-Rejected { Select-FlashPort '' @('COMx', 'COMy') 'other' }

    # Load the real backup reader's AST, mock only esptool. A simulated USB
    # failure at one 4 KB block must recover via 2 KB halves in byte order.
    $tokens = $null; $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
    Assert-Equal $parseErrors.Count 0
    $reader = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Read-FactoryRange' }, $true)
    . ([scriptblock]::Create($reader.Extent.Text))
    $temp = $folder; $common = @(); $readCommand = 'read-flash'
    $Esptool = Join-Path $folder 'fake-esptool.ps1'
    @'
param($Command, $Offset, $Length, $Destination)
$offsetNumber = [Convert]::ToInt32($Offset.Substring(2), 16)
$lengthNumber = [Convert]::ToInt32($Length.Substring(2), 16)
if ($lengthNumber -gt 2048 -and $offsetNumber -le 4096 -and ($offsetNumber + $lengthNumber) -gt 4096) {
    $global:LASTEXITCODE = 1; return
}
$bytes = [byte[]]::new($lengthNumber)
for ($index = 0; $index -lt $lengthNumber; $index++) { $bytes[$index] = [byte](($offsetNumber + $index) % 251) }
[IO.File]::WriteAllBytes($Destination, $bytes)
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath $Esptool -Encoding ASCII
    $backup = Join-Path $folder 'backup.bin'
    Read-FactoryRange 0 16384 $backup
    $actual = [IO.File]::ReadAllBytes($backup)
    Assert-Equal $actual.Length 16384
    for ($i = 0; $i -lt $actual.Length; $i++) {
        if ($actual[$i] -ne ($i % 251)) { throw 'Backup concatenation changed byte order.' }
    }
    $script:checks++
    # End-to-end DryRun must call only esptool's version query, never a device
    # command, and must remove its private firmware copy on success and error.
    if ($env:OS -eq 'Windows_NT') {
        @'
param($Command)
if ($Command -ne 'version') { throw 'A dry run tried to access a device.' }
Write-Output 'esptool v5.6.0'
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath $Esptool -Encoding ASCII
        $savedTemp = $env:TEMP
        $env:TEMP = $folder
        try {
            foreach ($mode in @('first', 'update', 'cleanup')) {
                $m = New-Fixture
                if ($mode -eq 'cleanup') { $m.kind = 'normal' }
                Save-Fixture $m
                $options = @{ Path = $folder; Port = 'COMx'; DryRun = $true; Esptool = $Esptool }
                if ($mode -eq 'first') { $options.FirstInstall = $true; $options.BackupDir = Join-Path $folder 'backups' }
                if ($mode -eq 'cleanup') { $options.Cleanup = $true }
                $transcript = (& $scriptPath @options 6>&1 | Out-String -Width 32767)
                $writeLine = ($transcript -split '\r?\n' | Where-Object { $_ -match 'write-flash' })
                $expectedCount = 1; if ($mode -eq 'first') { $expectedCount = 4 }
                Assert-Equal ([regex]::Matches($writeLine, '\.bin').Count) $expectedCount
                Assert-Equal ($transcript -match '--erase-all') $false
                $commands = @($transcript -split '\r?\n' | Where-Object { $_ -match '^DRY RUN:.*--chip' })
                Assert-Equal ($transcript.Contains('Tracker flash (development checkout).')) $true
                Assert-Equal ($commands[0] -match "'--before' '$(if ($mode -eq 'first') { 'no-reset' } else { 'default-reset' })'") $true
                Assert-Equal ($writeLine -match "'--before' 'no-reset' '--after' 'hard-reset'") $true
                Assert-Equal (@($commands | Where-Object { $_ -match 'erase-flash' }).Count) $(if ($mode -eq 'first') { 1 } else { 0 })
                if ($mode -eq 'first') {
                    Assert-Equal ($commands[-2] -match 'erase-flash') $true
                    Assert-Equal ($commands[-3] -match 'read-flash') $true
                    Assert-Equal ($transcript.Contains('LittleFS partition is fully erased; provisioning trip storage.')) $true
                    Assert-Equal ($transcript.Contains('Setup complete. Logging trips.')) $true
                }
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
            }
            # Apply the release builder's global byte replacement to the whole
            # script: the comparison must still identify a stamped release.
            $releaseScript = Join-Path $folder 'release-flash.ps1'
            [IO.File]::WriteAllText($releaseScript, $source.Replace(('@' + 'RELEASE_TAG' + '@'), 'v0.00-test'))
            $transcript = (& $releaseScript @options 6>&1 | Out-String -Width 32767)
            Assert-Equal ($transcript.Contains('Tracker flash (release v0.00-test).')) $true
            # Exercise the real SSH-copy branch with a function test double.
            # No executable scp/ssh is allowed to run in these tests.
            function scp {
                Assert-Equal $args[0] '-rq'
                $expectedRemote = 'alias:~/mileage-tracker/firmware-out/current/'
                if ($env:FLASH_TEST_UNRAID -eq '1') { $expectedRemote = 'root@host.invalid:/mnt/pool/appdata/mileage-tracker/firmware-out/current/' }
                Assert-Equal $args[1] $expectedRemote
                $destination = $args[2]
                if ($env:FLASH_TEST_COPY_FAILURE -eq '1') { Write-Error 'Synthetic Connection closed'; $global:LASTEXITCODE = 1; return }
                [IO.Directory]::CreateDirectory($destination) | Out-Null
                foreach ($file in @('manifest.json', 'bootloader.bin', 'partitions.bin', 'boot_app0.bin', 'app.bin')) {
                    [IO.File]::Copy((Join-Path $folder $file), (Join-Path $destination $file))
                }
                $global:LASTEXITCODE = 0
            }
            $scpResolver = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-FlashScp' }, $true)
            $copyScript = Join-Path $folder 'copy-flash.ps1'
            [IO.File]::WriteAllText($copyScript, $source.Replace($scpResolver.Extent.Text, "function Get-FlashScp { return 'scp' }"))
            try {
                $null = & $copyScript -Server alias -Port COMx -Esptool $Esptool -DryRun 6>&1
                $env:FLASH_TEST_UNRAID = '1'
                $null = & $copyScript -Server root@host.invalid -InstallDir /mnt/pool/appdata/mileage-tracker -Port COMx -Esptool $Esptool -DryRun 6>&1
                Remove-Item Env:\FLASH_TEST_UNRAID
                $env:FLASH_TEST_COPY_FAILURE = '1'
                $copyError = ''
                try { $null = & $copyScript -Server alias -Port COMx -Esptool $Esptool -DryRun 6>&1 2>&1 } catch { $copyError = $_.Exception.Message }
                Assert-Equal ($copyError.Contains('Firmware copy failed.')) $true
                Assert-Equal ($copyError.Contains('Git Bash')) $true
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
            } finally {
                Remove-Item Function:\scp
                Remove-Item Env:\FLASH_TEST_COPY_FAILURE -ErrorAction SilentlyContinue
                Remove-Item Env:\FLASH_TEST_UNRAID -ErrorAction SilentlyContinue
            }
            # Run the entire real flow with fake esptool and queued Read-Host
            # answers. Actual erase/write commands can only reach this fake.
            @'
$toolArgs = @($args)
@{ Arguments = [string[]]$toolArgs } | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:FLASH_TEST_LOG
$global:LASTEXITCODE = 0
if (($toolArgs -join ',') -eq 'version') { Write-Output ('esptool v' + $env:FLASH_TEST_MAJOR + '.6.0'); return }
if ('chip-id' -in $toolArgs -or 'chip_id' -in $toolArgs) {
    if ($env:FLASH_TEST_FAILURE -eq 'stderr') { Write-Error 'Synthetic diagnostic with a successful exit' }
    if ($env:FLASH_TEST_FAILURE -eq 'chip' -or
        ($env:FLASH_TEST_FAILURE -eq 'auto' -and ('default-reset' -in $toolArgs -or 'default_reset' -in $toolArgs))) {
        Write-Error 'Synthetic connection failure on stderr'
        $global:LASTEXITCODE = 1; return
    }
}
if (('write-flash' -in $toolArgs -or 'write_flash' -in $toolArgs) -and $env:FLASH_TEST_FAILURE -eq 'write') {
    $global:LASTEXITCODE = 1; return
}
if ('read-flash' -in $toolArgs -or 'read_flash' -in $toolArgs) {
    if ($env:FLASH_TEST_FAILURE -eq 'read') { $global:LASTEXITCODE = 1; return }
    $stream = [IO.File]::Create($toolArgs[-1])
    try {
        $size = 8388608; if ($env:FLASH_TEST_FAILURE -eq 'short') { $size = 17 }
        $stream.SetLength($size)
        if ($env:FLASH_TEST_FAILURE -eq 'mismatch' -and $toolArgs[-1].EndsWith('.verify.bin')) { $stream.WriteByte(1) }
    } finally { $stream.Dispose() }
}
if (('erase-flash' -in $toolArgs -or 'erase_flash' -in $toolArgs) -and $env:FLASH_TEST_FAILURE -eq 'erase') { $global:LASTEXITCODE = 1 }
'@ | Set-Content -LiteralPath $Esptool -Encoding ASCII
            function Read-Host {
                [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '', Justification = 'Test double for interactive prompts; removed in finally.')]
                param([string]$Prompt)
                if ($global:flashTestAnswers.Count -eq 0) { throw ('Unexpected prompt: ' + $Prompt) }
                return $global:flashTestAnswers.Dequeue()
            }
            function Invoke-MockedFlash($Options, [string[]]$Replies, [string]$Failure = '', [int]$Major = 5) {
                $global:flashTestAnswers = New-Object 'Collections.Generic.Queue[string]'
                foreach ($reply in $Replies) { $global:flashTestAnswers.Enqueue($reply) }
                $env:FLASH_TEST_LOG = Join-Path $folder 'commands.txt'
                [IO.File]::WriteAllText($env:FLASH_TEST_LOG, '')
                $env:FLASH_TEST_FAILURE = $Failure; $env:FLASH_TEST_MAJOR = [string]$Major
                $settings = @{ Path = $folder; Port = 'COMx'; Esptool = $Esptool; BackupDir = (Join-Path $folder 'backups') }
                foreach ($key in $Options.Keys) { $settings[$key] = $Options[$key] }
                $rejected = $false
                $transcript = ''; $errorMessage = ''
                try { $transcript = (& $scriptPath @settings 6>&1 2>&1 | Out-String -Width 32767) } catch { $rejected = $true; $errorMessage = $_.Exception.Message }
                $commands = @(Get-Content -LiteralPath $env:FLASH_TEST_LOG | ForEach-Object {
                    $_ | ConvertFrom-Json
                })
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
                return [pscustomobject]@{ Rejected = $rejected; Commands = $commands; Transcript = $transcript; ErrorMessage = $errorMessage }
            }
            $m = New-Fixture; Save-Fixture $m
            foreach ($major in @(4, 5)) {
                $suffix = '-flash'; if ($major -eq 4) { $suffix = '_flash' }
                foreach ($scenario in @(
                    @{ Options = @{ FirstInstall = $true }; Replies = @('', 'ERASE'); Reads = 2 },
                    @{ Options = @{}; Replies = @('yes', '', 'ERASE'); Reads = 2 },
                    @{ Options = @{ FirstInstall = $true; SkipBackup = $true }; Replies = @('', 'ERASE'); Reads = 0 }
                )) {
                    $run = Invoke-MockedFlash $scenario.Options $scenario.Replies '' $major
                    Assert-Equal $run.Rejected $false
                    Assert-Equal ($run.Commands[-2].Arguments -contains ('erase' + $suffix)) $true
                    Assert-Equal ($run.Commands[-1].Arguments -contains ('write' + $suffix)) $true
                    Assert-Equal (@($run.Commands | Where-Object { $_.Arguments -contains ('read' + $suffix) }).Count) $scenario.Reads
                    Assert-Equal (@($run.Commands[-1].Arguments | Where-Object { $_ -match '\.bin$' }).Count) 4
                    Assert-Equal (@($run.Commands | Where-Object { 'default-reset' -in $_.Arguments -or 'default_reset' -in $_.Arguments }).Count) 0
                }
            }
            foreach ($skip in @($false, $true)) {
                foreach ($reply in @('yes', 'erase', 'ERASE ', '')) {
                    $run = Invoke-MockedFlash @{ FirstInstall = $true; SkipBackup = $skip } @('', $reply)
                    Assert-Equal $run.Rejected $true
                    Assert-Equal (@($run.Commands | Where-Object { 'erase-flash' -in $_.Arguments -or 'write-flash' -in $_.Arguments }).Count) 0
                    Assert-Equal $global:flashTestAnswers.Count 0
                }
            }
            foreach ($failure in @('read', 'short', 'mismatch')) {
                $run = Invoke-MockedFlash @{ FirstInstall = $true } @('', 'ERASE') $failure
                Assert-Equal $run.Rejected $true
                Assert-Equal (@($run.Commands | Where-Object { 'erase-flash' -in $_.Arguments -or 'write-flash' -in $_.Arguments }).Count) 0
                Assert-Equal $global:flashTestAnswers.Count 1
            }
            $run = Invoke-MockedFlash @{ FirstInstall = $true } @('', 'ERASE') 'erase'
            Assert-Equal $run.Rejected $true
            Assert-Equal (@($run.Commands | Where-Object { 'write-flash' -in $_.Arguments }).Count) 0
            $m.kind = 'normal'; Save-Fixture $m
            foreach ($major in @(4, 5)) {
                $auto = 'default-reset'; $noReset = 'no-reset'; $hardReset = 'hard-reset'; $chip = 'chip-id'; $write = 'write-flash'
                if ($major -eq 4) { $auto = 'default_reset'; $noReset = 'no_reset'; $hardReset = 'hard_reset'; $chip = 'chip_id'; $write = 'write_flash' }
                foreach ($cleanup in @($false, $true)) {
                    $replies = @(); if (-not $cleanup) { $replies = @('no') }
                    $modeOptions = @{}; if ($cleanup) { $modeOptions.Cleanup = $true }
                    $run = Invoke-MockedFlash $modeOptions $replies '' $major
                    Assert-Equal $run.Rejected $false
                    Assert-Equal $run.Commands.Count 3
                    Assert-Equal ($run.Commands[1].Arguments -contains $auto) $true
                    Assert-Equal ($run.Transcript -match 'Download mode: hold BOOT') $false
                    Assert-Equal ($run.Commands[-1].Arguments -contains $noReset) $true
                    Assert-Equal ($run.Commands[-1].Arguments -contains $hardReset) $true
                    Assert-Equal (@($run.Commands[-1].Arguments | Where-Object { $_ -match '\.bin$' }).Count) 1
                    Assert-Equal $global:flashTestAnswers.Count 0
                    foreach ($failure in @('auto', 'chip')) {
                        $run = Invoke-MockedFlash $modeOptions ($replies + @('', 'COMy')) $failure $major
                        Assert-Equal $run.Rejected ($failure -eq 'chip')
                        Assert-Equal ($run.Commands[1].Arguments -contains $auto) $true
                        Assert-Equal ($run.Commands[2].Arguments -contains $chip) $true
                        Assert-Equal ($run.Commands[2].Arguments -contains $noReset) $true
                        Assert-Equal ($run.Commands[2].Arguments -contains 'COMy') $true
                        Assert-Equal (@($run.Commands | Where-Object { $write -in $_.Arguments }).Count) $(if ($failure -eq 'chip') { 0 } else { 1 })
                        if ($failure -eq 'auto') {
                            Assert-Equal ($run.Commands[-1].Arguments -contains 'COMy') $true
                            Assert-Equal (@($run.Commands[-1].Arguments | Where-Object { $_ -match '\.bin$' }).Count) 1
                        }
                        Assert-Equal $global:flashTestAnswers.Count 0
                    }
                    $run = Invoke-MockedFlash $modeOptions $replies 'write' $major
                    Assert-Equal $run.Rejected $true
                    Assert-Equal $run.Commands.Count 3
                    Assert-Equal $global:flashTestAnswers.Count 0
                    $run = Invoke-MockedFlash $modeOptions $replies 'stderr' $major
                    Assert-Equal $run.Rejected $false
                    Assert-Equal $run.Commands.Count 3
                    Assert-Equal $global:flashTestAnswers.Count 0
                }
            }
            foreach ($invalid in @(@{ SkipBackup = $true }, @{ Cleanup = $true; FirstInstall = $true })) {
                $run = Invoke-MockedFlash $invalid @('')
                Assert-Equal $run.Rejected $true
                Assert-Equal $run.Commands.Count 0
            }
            [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]@(1, 2, 3))
            Assert-Rejected { & $scriptPath @options }
            Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
        } finally {
            $env:TEMP = $savedTemp
            Remove-Item Function:\Read-Host -ErrorAction SilentlyContinue
            Remove-Variable flashTestAnswers -Scope Global -ErrorAction SilentlyContinue
            Remove-Item Env:\FLASH_TEST_LOG, Env:\FLASH_TEST_FAILURE, Env:\FLASH_TEST_MAJOR -ErrorAction SilentlyContinue
        }
    }
    Write-Output "PASS: $script:checks PowerShell flash safety checks (no device)."
} finally {
    Remove-Item -LiteralPath $folder -Recurse -Force
}

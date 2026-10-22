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
    foreach ($pair in @(@('bootloader', '0x0'), @('partitions', '0x8000'), @('boot_app0', '0xe000'), @('app', '0x10000'))) {
        $file = Join-Path $folder ($pair[0] + '.bin')
        [IO.File]::WriteAllBytes($file, [Text.Encoding]::ASCII.GetBytes($pair[0] + ' synthetic test bytes'))
        $parts += @{ name = $pair[0]; offset = $pair[1]; file = $pair[0] + '.bin'; sha256 = (Get-FileHash $file).Hash }
    }
    return [pscustomobject]@{
        schema = 1; chip = 'esp32s3'; kind = 'enroll'; version = 'v0.08+0123456789'; commit = '0' * 40
        fqbn = 'esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled'
        flash_mode = 'dio'; flash_freq = '80m'; flash_size = '8MB'; app_offset = '0x10000'; parts = $parts
    }
}
function Save-Fixture($Manifest) {
    $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $folder 'manifest.json') -Encoding ASCII
}
try {
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
    foreach ($change in @(@('file', '../app.bin'), @('sha256', ('0' * 64)), @('offset', '0x9000'),
        @('offset', '0x190000'), @('offset', '0x10001'), @('offset', '0x1;id'), @('name', 'bootloader'))) {
        $m = New-Fixture; $m.parts[3][$change[0]] = $change[1]; Save-Fixture $m
        Assert-Rejected { Test-FlashManifest $folder $false }
    }
    $m = New-Fixture; Save-Fixture $m
    [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]@(1, 2, 3))
    Assert-Rejected { Test-FlashManifest $folder $false }
    Remove-Item (Join-Path $folder 'app.bin')
    Assert-Rejected { Test-FlashManifest $folder $false }
    $m = New-Fixture
    [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]::new(0x180001))
    $m.parts[3].sha256 = (Get-FileHash (Join-Path $folder 'app.bin')).Hash; Save-Fixture $m
    Assert-Rejected { Test-FlashManifest $folder $false }
    $m = New-Fixture; $m.parts[2].offset = '0x9000'; Save-Fixture $m
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
                Assert-Equal (@($commands | Where-Object { $_ -match 'erase-flash' }).Count) $(if ($mode -eq 'first') { 1 } else { 0 })
                if ($mode -eq 'first') {
                    Assert-Equal ($commands[-2] -match 'erase-flash') $true
                    Assert-Equal ($commands[-3] -match 'read-flash') $true
                    Assert-Equal ($transcript.Contains('LittleFS partition is fully erased; provisioning trip storage.')) $true
                    Assert-Equal ($transcript.Contains('Setup complete. Logging trips.')) $true
                }
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
            }
            # Run the entire real flow with fake esptool and queued Read-Host
            # answers. Actual erase/write commands can only reach this fake.
            @'
$toolArgs = @($args)
@{ Arguments = [string[]]$toolArgs } | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:FLASH_TEST_LOG
$global:LASTEXITCODE = 0
if (($toolArgs -join ',') -eq 'version') { Write-Output ('esptool v' + $env:FLASH_TEST_MAJOR + '.6.0'); return }
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
                try { $null = & $scriptPath @settings 6>&1 } catch { $rejected = $true }
                $commands = @(Get-Content -LiteralPath $env:FLASH_TEST_LOG | ForEach-Object {
                    $_ | ConvertFrom-Json
                })
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
                return [pscustomobject]@{ Rejected = $rejected; Commands = $commands }
            }
            $m = New-Fixture; Save-Fixture $m
            foreach ($major in @(4, 5)) {
                $suffix = '-flash'; if ($major -eq 4) { $suffix = '_flash' }
                foreach ($scenario in @(
                    @{ Options = @{ FirstInstall = $true }; Replies = @('', 'ERASE'); Reads = 2 },
                    @{ Options = @{}; Replies = @('', 'yes', 'ERASE'); Reads = 2 },
                    @{ Options = @{ FirstInstall = $true; SkipBackup = $true }; Replies = @('', 'ERASE'); Reads = 0 }
                )) {
                    $run = Invoke-MockedFlash $scenario.Options $scenario.Replies '' $major
                    Assert-Equal $run.Rejected $false
                    Assert-Equal ($run.Commands[-2].Arguments -contains ('erase' + $suffix)) $true
                    Assert-Equal ($run.Commands[-1].Arguments -contains ('write' + $suffix)) $true
                    Assert-Equal (@($run.Commands | Where-Object { $_.Arguments -contains ('read' + $suffix) }).Count) $scenario.Reads
                    Assert-Equal (@($run.Commands[-1].Arguments | Where-Object { $_ -match '\.bin$' }).Count) 4
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
            foreach ($scenario in @(@{ Options = @{}; Replies = @('', 'no') }, @{ Options = @{ Cleanup = $true }; Replies = @('') })) {
                $run = Invoke-MockedFlash $scenario.Options $scenario.Replies
                Assert-Equal $run.Rejected $false
                Assert-Equal (@($run.Commands | Where-Object { 'erase-flash' -in $_.Arguments -or 'read-flash' -in $_.Arguments }).Count) 0
                Assert-Equal (@($run.Commands[-1].Arguments | Where-Object { $_ -match '\.bin$' }).Count) 1
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

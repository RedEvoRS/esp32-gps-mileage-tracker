# Plain PowerShell tests; no Pester/module dependency and no serial access.
# Run: powershell -ExecutionPolicy Bypass -File install/tests/flash.Tests.ps1
# Or:  pwsh -File install/tests/flash.Tests.ps1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', '', Justification = 'The dynamically loaded backup reader uses temp, common and readCommand from this scope.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Tests create only disposable fixtures in their own temporary directory.')]
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
    if ($Actual -cne $Expected) { throw "Expected '$Expected'; got '$Actual'." }
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
                Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
            }
            [IO.File]::WriteAllBytes((Join-Path $folder 'app.bin'), [byte[]]@(1, 2, 3))
            Assert-Rejected { & $scriptPath @options }
            Assert-Equal (@(Get-ChildItem $folder -Directory -Filter 'tracker-flash-*').Count) 0
        } finally { $env:TEMP = $savedTemp }
    }
    Write-Output "PASS: $script:checks PowerShell flash safety checks (no device)."
} finally {
    Remove-Item -LiteralPath $folder -Recurse -Force
}

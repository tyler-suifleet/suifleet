# Runs only on an elevated disposable Windows software runner. No cloud/chain writes.
param(
    [string]$Daemon = "$PSScriptRoot/../suifleet-edge/target/debug/suifleet.exe",
    [string]$Fixture = "$PSScriptRoot/../suifleet-edge/target/debug/examples/component.exe",
    [string]$Evidence = "$PSScriptRoot/../windows-evidence.json"
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Elevated disposable runner required' }
if (Get-Service SuiFleet -ErrorAction SilentlyContinue) { throw 'Runner already has SuiFleet installed; refusing existing service' }
$serviceCreated = $false
$userCreated = $false
$outsiderCreated = $false
$root = Join-Path $env:ProgramData ('SuiFleet-Test-' + [guid]::NewGuid().ToString('N'))
$user = 'sf' + [guid]::NewGuid().ToString('N').Substring(0,12)
$outsider = 'sf' + [guid]::NewGuid().ToString('N').Substring(0,12)
$password = 'Sf!9' + [guid]::NewGuid().ToString('N')
$otherPassword = 'Sf!8' + [guid]::NewGuid().ToString('N')
$pipe = 'suifleet-' + [guid]::NewGuid().ToString('N')
$checks = [Collections.Generic.List[string]]::new()
$script:lastRequest = 'none'
function Assert-True($value, [string]$message) { if (-not $value) { throw $message } }
function Wait-Until([scriptblock]$condition, [string]$message, [int]$seconds = 25) {
    $deadline = [DateTime]::UtcNow.AddSeconds($seconds)
    do { if (& $condition) { return }; Start-Sleep -Milliseconds 150 } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timeout: $message; last request: $script:lastRequest"
}
function Request($request, [bool]$success = $true) {
    $request | ConvertTo-Json -Depth 12 -Compress | Set-Content -Encoding utf8NoBOM (Join-Path $root 'request.json')
    $output = & (Join-Path $root 'suifleet.exe') --request $pipe (Join-Path $root 'request.json') 2>&1
    $code = $LASTEXITCODE
    $script:lastRequest = "exit=$code; $output"
    if ($success -and $code -ne 0) { throw "Request failed: $output" }
    if (-not $success -and $code -eq 0) { throw 'Expected request rejection' }
    if ($output) { return (($output | Where-Object { $_ -is [string] }) -join "`n" | ConvertFrom-Json) }
}
function Status { try { return (Request @{operation='status'}).status } catch { return $null } }
function Work { return Join-Path $root "components/$digest/work" }
function Check-Dead([int[]]$ids) {
    Wait-Until { @($ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }).Count -eq 0 } 'component descendants still alive'
}
function Process-Ids {
    return @([int](Get-Content (Join-Path (Work) 'parent.pid')), [int](Get-Content (Join-Path (Work) 'descendant.pid')))
}
function Wait-Component {
    Wait-Until { (Test-Path (Join-Path (Work) 'denials.json')) -and (Test-Path (Join-Path (Work) 'descendant.pid')) } 'component execution/denials'
    $denials = Get-Content (Join-Path (Work) 'denials.json') | ConvertFrom-Json
    Assert-True ($denials.secret -and $denials.state_write -and $denials.breakaway -and $denials.operator) 'component escaped ACL/caller/job restrictions'
}
try {
    New-Item -ItemType Directory $root | Out-Null
    # Protect before any credentials are written; root is unique and not a reparse path.
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*$($identity.User.Value):(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'initial ACL provisioning failed' }
    New-LocalUser $user -Password (ConvertTo-SecureString $password -AsPlainText -Force) -AccountNeverExpires | Out-Null
    $userCreated = $true
    New-LocalUser $outsider -Password (ConvertTo-SecureString $otherPassword -AsPlainText -Force) -AccountNeverExpires | Out-Null
    $outsiderCreated = $true
    # Explicitly add Users by SID, independent of host localization.
    $users = Get-LocalGroup -SID 'S-1-5-32-545'
    Add-LocalGroupMember -Group $users -Member $user,$outsider
    $sid = (Get-LocalUser $user).SID.Value
    Copy-Item (Resolve-Path $Daemon) (Join-Path $root 'suifleet.exe')
    Copy-Item (Resolve-Path $Fixture) (Join-Path $root 'component.exe')
    @{ root=$root; pipe=$pipe; operator_sid=$identity.User.Value; component_sid=$sid; component_user=$user; component_password=$password } | ConvertTo-Json | Set-Content -Encoding utf8NoBOM (Join-Path $root 'config.json')
    $binaryPath = '"' + (Join-Path $root 'suifleet.exe') + '" --service "' + (Join-Path $root 'config.json') + '"'
    Assert-True (-not (Get-Service SuiFleet -ErrorAction SilentlyContinue)) 'runner already has SuiFleet installed'
    New-Service -Name SuiFleet -BinaryPathName $binaryPath -StartupType Manual | Out-Null
    $serviceCreated = $true
    Start-Service SuiFleet
    Wait-Until { $s = Status; $null -ne $s } 'SCM service ready'
    $checks.Add('SCM start and authenticated operator status')
    $digest = (Get-FileHash (Join-Path $root 'component.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = @{ schema_version=1; name='test.app'; version='1'; platform='windows-x86_64'; sha256=$digest; run=@('run',$root,$pipe); install=@('install'); shutdown=@('shutdown'); timeout_seconds=8; max_restarts=1 }
    Request @{operation='install';manifest=$manifest;artifact=(Join-Path $root 'component.exe')} | Out-Null
    Wait-Component
    Assert-True (Test-Path (Join-Path (Work) 'installed')) 'install lifecycle did not execute'
    $checks.Add('real component install/run; secret read, state write, operator IPC and breakaway denied')
    # An unrelated local user cannot even open the named pipe or read configuration.
    $probe = Join-Path $env:PUBLIC ('SuiFleet-Probe-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $probe | Out-Null
    & icacls.exe $probe /grant '*S-1-5-32-545:(OI)(CI)M' | Out-Null
    Copy-Item (Resolve-Path $Fixture) (Join-Path $probe 'probe.exe')
    $credential = [pscredential]::new("$env:COMPUTERNAME\$outsider", (ConvertTo-SecureString $otherPassword -AsPlainText -Force))
    $probeProcess = Start-Process (Join-Path $probe 'probe.exe') -ArgumentList @('probe',$pipe,('"' + (Join-Path $root 'config.json') + '"'),('"' + (Join-Path $probe 'result') + '"')) -Credential $credential -PassThru
    Assert-True ($probeProcess.WaitForExit(15000)) 'outsider probe hung'
    Assert-True ((Get-Content (Join-Path $probe 'result')) -eq 'denied') 'outsider passed pipe/file ACL'
    $checks.Add('unconfigured local caller denied at pipe and secret filesystem ACL')
    $bad = $manifest.Clone(); $bad.sha256 = '00' * 32
    Request @{operation='install';manifest=$bad;artifact=(Join-Path $root 'component.exe')} $false | Out-Null
    $bad = $manifest.Clone(); $bad.platform = 'linux-x86_64'
    Request @{operation='install';manifest=$bad;artifact=(Join-Path $root 'component.exe')} $false | Out-Null
    Assert-True (Status).running 'preflight rejection stopped active component'
    $checks.Add('wrong digest and incompatible platform rejected before lifecycle mutation')
    $ids = Process-Ids
    Request @{operation='stop'} | Out-Null
    Check-Dead $ids
    Assert-True (Test-Path (Join-Path (Work) 'shutdown')) 'shutdown lifecycle missing'
    $checks.Add('explicit shutdown and complete Job Object descendant termination')
    Remove-Item (Join-Path (Work) '*.pid'),(Join-Path (Work) 'denials.json')
    Request @{operation='start'} | Out-Null
    Wait-Component
    $ids = Process-Ids
    Stop-Service SuiFleet
    Check-Dead $ids
    Remove-Item (Join-Path (Work) '*.pid'),(Join-Path (Work) 'denials.json')
    Start-Service SuiFleet
    Wait-Component
    $checks.Add('SCM stop kills descendants; restart bootstraps persisted desired component')
    # Kill the service process, then explicitly restart via SCM. Job handles must close.
    $ids = Process-Ids
    $servicePid = (Get-CimInstance Win32_Service -Filter "Name='SuiFleet'").ProcessId
    Stop-Process -Id $servicePid -Force
    Check-Dead $ids
    Wait-Until { (Get-Service SuiFleet).Status -eq 'Stopped' } 'service process crash status'
    Remove-Item (Join-Path (Work) '*.pid'),(Join-Path (Work) 'denials.json')
    Start-Service SuiFleet
    Wait-Component
    $checks.Add('service crash closes Job Object and restart has no old descendants')
    # Install failure and timeout are durable failures and never start the run phase.
    $failed = $manifest.Clone(); $failed.install = @('fail')
    Request @{operation='install';manifest=$failed;artifact=(Join-Path $root 'component.exe')} $false | Out-Null
    Assert-True ((Status).phase -eq 'failed') 'failed install not durable'
    $failed.install = @('hang'); $failed.timeout_seconds = 1
    Request @{operation='install';manifest=$failed;artifact=(Join-Path $root 'component.exe')} $false | Out-Null
    Assert-True ((Status).phase -eq 'failed') 'install timeout not recorded'
    $checks.Add('nonzero install and bounded timeout fail without starting component')
    # Run crash: one restart permitted, all grandchildren of both runs must disappear.
    Request @{operation='install';manifest=$manifest;artifact=(Join-Path $root 'component.exe')} | Out-Null
    Wait-Until { $s=Status; $null -ne $s -and $s.running } 'reinstalled run'
    $crash = $manifest.Clone(); $crash.run = @('run',$root,$pipe,'crash')
    Request @{operation='install';manifest=$crash;artifact=(Join-Path $root 'component.exe')} | Out-Null
    Wait-Until { $s=Status; $null -ne $s -and $s.phase -eq 'failed' -and $s.restarts -eq 1 } 'restart budget exhaustion'
    Check-Dead (Process-Ids)
    $checks.Add('component crash contains descendants and exhausts bounded restart policy')
    # Kill the service during a deliberately long install. Never automatically replay it.
    $hanging = $manifest.Clone(); $hanging.install = @('hang'); $hanging.timeout_seconds = 120
    Remove-Item (Join-Path (Work) 'install-started') -ErrorAction SilentlyContinue
    @{operation='install';manifest=$hanging;artifact=(Join-Path $root 'component.exe')} | ConvertTo-Json -Depth 12 -Compress | Set-Content -Encoding utf8NoBOM (Join-Path $root 'hang.json')
    $client = Start-Process (Join-Path $root 'suifleet.exe') -ArgumentList @('--request',$pipe,('"' + (Join-Path $root 'hang.json') + '"')) -PassThru -WindowStyle Hidden
    Wait-Until { Test-Path (Join-Path (Work) 'install-started') } 'long install started'
    $stamp = (Get-Item (Join-Path (Work) 'install-started')).LastWriteTimeUtc
    $servicePid = (Get-CimInstance Win32_Service -Filter "Name='SuiFleet'").ProcessId
    Stop-Process -Id $servicePid -Force
    Wait-Until { (Get-Service SuiFleet).Status -eq 'Stopped' } 'interrupted service stopped'
    Start-Service SuiFleet
    Wait-Until { $s=Status; $null -ne $s -and $s.phase -eq 'interrupted' } 'interrupted install quarantined'
    Request @{operation='start'} $false | Out-Null
    Request @{operation='stop'} | Out-Null
    Request @{operation='start'} $false | Out-Null
    Assert-True ((Get-Item (Join-Path (Work) 'install-started')).LastWriteTimeUtc -eq $stamp) 'ambiguous install replayed'
    $checks.Add('interrupted install survives SCM restart without replay or implicit start')
    Request @{operation='install';manifest=$manifest;artifact=(Join-Path $root 'component.exe')} | Out-Null
    Wait-Component
    $checks.Add('explicit operator reinstall recovers interrupted lifecycle')
    $sourceHead = & git rev-parse HEAD
    $baseHead = $null
    if ($env:GITHUB_EVENT_PATH) {
        $event = Get-Content $env:GITHUB_EVENT_PATH | ConvertFrom-Json
        if ($event.PSObject.Properties.Name -contains 'pull_request') { $sourceHead = $event.pull_request.head.sha; $baseHead = $event.pull_request.base.sha }
    }
    [ordered]@{ verdict='passed'; platform=[Environment]::OSVersion.VersionString; architecture=$env:PROCESSOR_ARCHITECTURE; rust=(& rustc +1.94.0 --version); head=$sourceHead; base=$baseHead; tested_commit=(& git rev-parse HEAD); checks=$checks; limits=@('No AWS/live-chain/physical/production acceptance', 'Local operator manifest; Linux chain reconciliation and rich IPC APIs explicitly unsupported on Windows') } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8NoBOM $Evidence
    Write-Host ($checks -join "`n")
} finally {
    if ($serviceCreated) { Stop-Service SuiFleet -Force -ErrorAction SilentlyContinue; & sc.exe delete SuiFleet | Out-Null }
    if ($userCreated) { Remove-LocalUser $user -ErrorAction SilentlyContinue }
    if ($outsiderCreated) { Remove-LocalUser $outsider -ErrorAction SilentlyContinue }
    if (Test-Path $root) { Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue }
    if (Get-Variable probe -ErrorAction SilentlyContinue) { Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue }
}

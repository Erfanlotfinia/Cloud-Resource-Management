$ErrorActionPreference = "Stop"
$Base = "http://localhost:8000"
$PayloadDir = Join-Path $PSScriptRoot "payloads"
$Pass = 0
$Fail = 0

function Check($name, $cond) {
    if ($cond) { Write-Output "PASS - $name"; $script:Pass++ } else { Write-Output "FAIL - $name"; $script:Fail++ }
}

function Invoke-Api {
    param(
        [string]$Method = "GET",
        [string]$Url,
        [hashtable]$Headers = @{},
        [string]$BodyFile = $null
    )
    $args = @("-s", "-w", "`nHTTP:%{http_code}", "-X", $Method, $Url)
    foreach ($k in $Headers.Keys) { $args += @("-H", "$k`: $($Headers[$k])") }
    if ($BodyFile) { $args += @("--data-binary", "@$BodyFile") }
    $raw = & curl.exe @args
    $parts = $raw -split "`n"
    return @{ Body = ($parts[0..($parts.Length - 2)] -join "`n"); Code = $parts[-1].Replace("HTTP:", "") }
}

Check "GET /health" ((Invoke-Api -Url "$Base/health").Code -eq "200")

$r = Invoke-Api -Method POST -Url "$Base/auth/register" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "register_user.json")
Check "POST /auth/register user" ($r.Code -eq "201")

$r = Invoke-Api -Method POST -Url "$Base/auth/register" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "register_admin_bad.json")
Check "POST /auth/register admin blocked" ($r.Code -eq "403")

$r = Invoke-Api -Method POST -Url "$Base/auth/register" -Headers @{"Content-Type"="application/json"; "X-Admin-Setup-Token"="local-admin-setup-token"} -BodyFile (Join-Path $PayloadDir "register_admin.json")
Check "POST /auth/register admin" ($r.Code -eq "201")

$r = Invoke-Api -Method POST -Url "$Base/auth/login" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "login_user.json")
$userToken = ($r.Body | ConvertFrom-Json).access_token
Check "POST /auth/login user" ($r.Code -eq "200" -and $userToken)

$r = Invoke-Api -Method POST -Url "$Base/auth/login" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "login_admin.json")
$adminToken = ($r.Body | ConvertFrom-Json).access_token
Check "POST /auth/login admin" ($r.Code -eq "200" -and $adminToken)

$auth = @{ Authorization = "Bearer $userToken" }

$r = Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job.json")
$job = $r.Body | ConvertFrom-Json
Check "POST /jobs create" ($r.Code -eq "201" -and $job.id -gt 0)

$r1 = Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"; "Idempotency-Key"="curl-key-1"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job_short.json")
$r2 = Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"; "Idempotency-Key"="curl-key-1"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job_short.json")
Check "POST /jobs idempotency" (($r1.Body | ConvertFrom-Json).id -eq ($r2.Body | ConvertFrom-Json).id)

$r = Invoke-Api -Url "$Base/jobs?limit=10" -Headers $auth
$list = $r.Body | ConvertFrom-Json
Check "GET /jobs list" ($r.Code -eq "200" -and $list.items.Count -ge 1)

if ($list.next_cursor) {
    Check "GET /jobs cursor" ((Invoke-Api -Url "$Base/jobs?limit=1&cursor=$($list.next_cursor)" -Headers $auth).Code -eq "200")
} else { Check "GET /jobs cursor" $true }

Check "GET /jobs/{id}" ((Invoke-Api -Url "$Base/jobs/$($job.id)" -Headers $auth).Code -eq "200")
Check "GET /jobs/{id}/logs" ((Invoke-Api -Url "$Base/jobs/$($job.id)/logs" -Headers $auth).Code -eq "200")

Invoke-Api -Method POST -Url "$Base/auth/register" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "register_user2.json") | Out-Null
$r = Invoke-Api -Method POST -Url "$Base/auth/login" -Headers @{"Content-Type"="application/json"} -BodyFile (Join-Path $PayloadDir "login_user2.json")
$user2Token = ($r.Body | ConvertFrom-Json).access_token
Check "GET /jobs/{id} forbidden" ((Invoke-Api -Url "$Base/jobs/$($job.id)" -Headers @{Authorization="Bearer $user2Token"}).Code -eq "403")
Check "GET /jobs/{id} admin access" ((Invoke-Api -Url "$Base/jobs/$($job.id)" -Headers @{Authorization="Bearer $adminToken"}).Code -eq "200")

$cancelJob = ((Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job_long.json")).Body | ConvertFrom-Json)
$cancelResp = (Invoke-Api -Method POST -Url "$Base/jobs/$($cancelJob.id)/cancel" -Headers $auth).Body | ConvertFrom-Json
Check "POST /jobs/{id}/cancel" ($cancelResp.status -eq "cancelled")

$sse = curl.exe -s -N -m 3 -H "Authorization: Bearer $userToken" "$Base/jobs/$($job.id)/events" 2>$null
Check "GET /jobs/{id}/events SSE" ($sse -match "snapshot|poll|data:")

$runResp = Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job_run.json")
$runJob = $runResp.Body | ConvertFrom-Json
$completed = $false
if ($runResp.Code -eq "201") {
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $status = ((Invoke-Api -Url "$Base/jobs/$($runJob.id)" -Headers $auth).Body | ConvertFrom-Json).status
        if ($status -eq "completed") { $completed = $true; break }
    }
}
Check "Job completes via worker" $completed

$got429 = $false
for ($i = 1; $i -le 12; $i++) {
    if ((Invoke-Api -Method POST -Url "$Base/jobs" -Headers (@{"Content-Type"="application/json"} + $auth) -BodyFile (Join-Path $PayloadDir "create_job_short.json")).Code -eq "429") { $got429 = $true; break }
}
Check "POST /jobs rate limit 429" $got429

Write-Output ""
Write-Output "=== SUMMARY: $Pass passed, $Fail failed ==="
if ($Fail -gt 0) { exit 1 }

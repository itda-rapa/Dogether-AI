#
# 파이프라인 디버그 트레이서 실행기 (PowerShell)
#
# FastAPI/Swagger 를 띄우지 않고, 약속 추출 파이프라인을 로컬에서 한 노드씩
# 태워보며 전 과정을 보여줍니다. 실제 Gemma 엔드포인트를 호출하므로 .env 가
# 응답해야 합니다.
#
# 사용 예:
#   .\tests\cli_debug_test\run.ps1 -Scenario outing
#   .\tests\cli_debug_test\run.ps1 -Scenario walk -NoThink
#   .\tests\cli_debug_test\run.ps1 -File .\my_convo.json -Full
#   .\tests\cli_debug_test\run.ps1 -List
#

param(
    [string]$Scenario = "walk",
    [string]$File,
    [switch]$NoThink,
    [switch]$Full,
    [switch]$List
)

$ErrorActionPreference = "Stop"

# 콘솔을 UTF-8 로 맞춰 한글이 깨지지 않게 합니다.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONIOENCODING = "utf-8"

# 프로젝트 루트(ai-server-m1)를 찾습니다.
# 후보: (1) 스크립트 폴더의 두 단계 위, (2) 현재 위치에서 위로 올라가며 .venv 탐색.
function Find-ProjectRoot {
    $candidates = @()
    if ($PSScriptRoot) {
        $candidates += (Join-Path $PSScriptRoot "..\..")
    }
    $candidates += (Get-Location).Path

    foreach ($c in $candidates) {
        $probe = $c
        while ($probe) {
            if (Test-Path (Join-Path $probe ".venv\Scripts\python.exe")) {
                return (Resolve-Path $probe).Path
            }
            $parent = Split-Path -Parent $probe
            if (-not $parent -or $parent -eq $probe) { break }
            $probe = $parent
        }
    }
    return $null
}

$root = Find-ProjectRoot
if (-not $root) {
    Write-Error "프로젝트 루트(.venv 포함)를 찾지 못했습니다. ai-server-m1 폴더 안에서 실행하세요."
    exit 1
}

$py = Join-Path $root ".venv\Scripts\python.exe"
$traceScript = Join-Path $root "tests\cli_debug_test\pipeline_trace.py"

if (-not (Test-Path $py)) {
    Write-Error "가상환경 파이썬을 찾지 못했습니다: $py"
    exit 1
}

# 인자 구성
$argsList = @()
if ($List) {
    $argsList += "--list"
} else {
    if ($File) {
        $argsList += @("--file", $File)
    } else {
        $argsList += @("--scenario", $Scenario)
    }
    if ($NoThink) { $argsList += "--no-think" }
    if ($Full)    { $argsList += "--full" }
}

# import 경로(app, tests)가 잡히도록 프로젝트 루트에서 실행
Push-Location $root
try {
    & $py $traceScript @argsList
} finally {
    Pop-Location
}

<#
.SYNOPSIS
  공단 문서·관광 POI 데이터를 주기적으로 갱신하는 오케스트레이션 스크립트.

.DESCRIPTION
  1. data-pipeline (Python): 증분 크롤링 → 구조화 → 전처리 → documents.jsonl 생성
  2. documents.jsonl을 저장소 루트로 복사 (ingest.js가 루트 파일을 읽음)
  3. node ingest.js: documents 테이블 재적재 (청킹·임베딩·Supabase)
  4. node patch_reserve_links.js: ingest.js 재적재로 사라진 예약 링크 재주입
  5. (-IncludeTour 지정 시) node ingest_tour.js: 관광 POI 갱신
  6. node check_db.js: 적재 결과 검증

  로컬(또는 개인 서버)에서 스케줄러로 주기 실행하기 위한 스크립트다.
  크롤링 원본에 개인정보가 섞여 있어 GitHub Actions 등 외부 CI에는 올리지 않는다.

.PARAMETER IncludeTour
  관광 POI(ingest_tour.js)까지 함께 갱신한다. TourAPI 데이터는 자주 바뀌지 않으므로
  기본은 끄고, 분기(3개월)마다 한 번씩만 이 스위치를 켜서 실행하는 걸 권장한다.

.EXAMPLE
  # 매주 실행 (공지·게시글 갱신만)
  powershell -File refresh_data.ps1

.EXAMPLE
  # 분기 1회 실행 (관광 POI까지 갱신)
  powershell -File refresh_data.ps1 -IncludeTour

.NOTES
  스케줄 등록 예시(schtasks, 관리자 권한 불필요):

    # 매주 일요일 새벽 3시 — 공지 갱신
    schtasks /create /tn "g-chat-refresh-weekly" ^
      /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo-path>\refresh_data.ps1\"" ^
      /sc WEEKLY /d SUN /st 03:00

    # 3개월마다 — 관광 POI 포함 갱신 (매월 1일 새벽 4시 트리거 중 3개월 간격만 실행)
    schtasks /create /tn "g-chat-refresh-quarterly" ^
      /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo-path>\refresh_data.ps1\" -IncludeTour" ^
      /sc MONTHLY /mo 3 /d 1 /st 04:00

  등록된 작업 확인/삭제:
    schtasks /query /tn "g-chat-refresh-weekly"
    schtasks /delete /tn "g-chat-refresh-weekly" /f
#>

param(
    [switch]$IncludeTour
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("refresh_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

function Invoke-Step {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$Args,
        [string]$WorkDir = $RepoRoot
    )
    Write-Log "=== $Name 시작 ($Exe $($Args -join ' ')) ==="
    Push-Location $WorkDir
    try {
        & $Exe @Args 2>&1 | ForEach-Object {
            Write-Output $_
            Add-Content -Path $LogFile -Value $_
        }
        if ($LASTEXITCODE -ne 0) {
            throw "$Name 실패 (exit code $LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }
    Write-Log "=== $Name 완료 ==="
}

try {
    Write-Log "########## 데이터 갱신 시작 (IncludeTour=$IncludeTour) ##########"

    Invoke-Step -Name "크롤링·정제 (data-pipeline)" -Exe "python" -Args @("main.py") `
        -WorkDir (Join-Path $RepoRoot "data-pipeline")

    $src = Join-Path $RepoRoot "data-pipeline\data\processed\documents.jsonl"
    $dst = Join-Path $RepoRoot "documents.jsonl"
    Write-Log "documents.jsonl 복사: $src -> $dst"
    Copy-Item -Path $src -Destination $dst -Force

    Invoke-Step -Name "Supabase 적재 (ingest.js)" -Exe "node" -Args @("ingest.js")
    Invoke-Step -Name "예약 링크 재주입 (patch_reserve_links.js)" -Exe "node" -Args @("patch_reserve_links.js")

    if ($IncludeTour) {
        Invoke-Step -Name "관광 POI 갱신 (ingest_tour.js)" -Exe "node" -Args @("ingest_tour.js")
    }

    Invoke-Step -Name "적재 검증 (check_db.js)" -Exe "node" -Args @("check_db.js")

    Write-Log "########## 데이터 갱신 완료 ##########"
}
catch {
    Write-Log "!!! 갱신 실패: $($_.Exception.Message)"
    throw
}

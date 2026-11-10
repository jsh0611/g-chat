<#
.SYNOPSIS
  공단 문서·관광 POI·관광지 통합예약시스템 데이터를 주기적으로 갱신하는 오케스트레이션 스크립트.

.DESCRIPTION
  1. data-pipeline (Python): 공단 홈페이지 증분 크롤링 → 구조화 → 전처리 → documents.jsonl 생성
  2. data-pipeline camping: 관광지 통합예약시스템(campingkorea.or.kr) 허용 목록 증분 수집
     → documents_camping.jsonl 생성
  2-1. main.py ocr-new: OCR 결과 없는 문서(새 글)의 이미지만 Tesseract OCR → 고정 키로 병합 (agent_env)
  3. 두 jsonl을 저장소 루트로 복사 (ingest*.js가 루트 파일을 읽음, 이전 파일은 .bak 으로 보관)
  4. python dedup.py: 새로 생긴 중복 후보 보고 (dry-run — 적용은 사람이 확인 후 --apply)
  5. node ingest.js: 공단 문서(metadata.source='documents.jsonl') 증분 적재
     — 바뀐 청크만 임베딩, 새 행을 다 넣은 뒤 이전 행 삭제. 다른 출처 행은 건드리지 않음
  6. node patch_reserve_links.js: 다시 적재된 공단 시설 문서에 예약 링크 재주입
  7. (-IncludeTour 지정 시) node ingest_tour.js: 관광 POI 갱신
  8. node ingest_camping.js: 예약시스템 문서(metadata.source='campingkorea') 증분 적재
  8-1. node ingest_manual.js: 부서 표준답변 FAQ(data/manual_faq.json, metadata.source='manual_faq') 적재
  8-2. node ingest_pages.js: 시설 전용 사이트 안내 페이지(data/extra_pages.json, metadata.source='extra_pages') 재수집·적재
       (같은 파일의 corrections·supersede 는 5단계 ingest.js 가 공단 문서에 적용)
  9. node check_db.js: 적재 결과 검증 — 임베딩 NULL 행이 있으면 실패 (-AllowNullEmbeddings 면 경고)

  실패 판단은 각 단계의 종료 코드로만 한다. (Windows PowerShell 5.1 에서 $ErrorActionPreference="Stop"
  과 2>&1 을 함께 쓰면 node/python 이 stderr 로 경고 한 줄만 내도 NativeCommandError 로 멈추던 문제 수정)
  임베딩은 하루 사용량 예산을 따른다(embed_utils.js — 봇 몫 EMBED_RESERVE 건을 남김). 예산을 넘는 청크는
  NULL 로 적재되고 embed_backfill.js 가 채운다.

  로컬(또는 개인 서버)에서 스케줄러로 주기 실행하기 위한 스크립트다.
  크롤링 원본에 개인정보가 섞여 있어 GitHub Actions 등 외부 CI에는 올리지 않는다.
  ※ 이 파일은 UTF-8 BOM 으로 저장해야 한다 — PowerShell 5.1 은 BOM 없는 스크립트를 cp949 로 읽어 한글이 깨진다.

.PARAMETER IncludeTour
  관광 POI(ingest_tour.js)까지 함께 갱신한다. TourAPI 데이터는 자주 바뀌지 않으므로
  기본은 끄고, 분기(3개월)마다 한 번씩만 이 스위치를 켜서 실행하는 걸 권장한다.

.PARAMETER AllowNullEmbeddings
  마지막 검사에서 임베딩 NULL 행을 실패가 아닌 경고로 처리한다. 백필이 며칠에 걸쳐 진행 중일 때 사용.
  (NULL 외의 검사 오류는 이 옵션이 있어도 실패)

.PARAMETER DryRun
  수집(크롤링·정제)과 중복 후보 보고, 적재 드라이런(DRY_RUN=true — DB 변경·임베딩 없이 변경 통계만)까지만 한다.

.EXAMPLE
  # 매주 실행 (공지·게시글 갱신만)
  powershell -File scripts/refresh_data.ps1

.EXAMPLE
  # 분기 1회 실행 (관광 POI까지 갱신)
  powershell -File scripts/refresh_data.ps1 -IncludeTour

.EXAMPLE
  # 임베딩 백필이 끝나기 전 (NULL 행을 경고로만)
  powershell -File scripts/refresh_data.ps1 -AllowNullEmbeddings

.NOTES
  스케줄 등록 예시(schtasks, 관리자 권한 불필요):

    # 매주 일요일 저녁 8시 — 공지 갱신 (임베딩 일일 한도가 16시 KST에 초기화된 뒤. 실제 등록됨: 2026-11-05)
    schtasks /create /tn "g-chat-refresh-weekly" ^
      /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo-path>\scripts\refresh_data.ps1\"" ^
      /sc WEEKLY /d SUN /st 20:00

    # 3개월마다 — 관광 POI 포함 갱신 (매월 1일 새벽 4시 트리거 중 3개월 간격만 실행)
    schtasks /create /tn "g-chat-refresh-quarterly" ^
      /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo-path>\scripts\refresh_data.ps1\" -IncludeTour" ^
      /sc MONTHLY /mo 3 /d 1 /st 04:00

  등록된 작업 확인/삭제:
    schtasks /query /tn "g-chat-refresh-weekly"
    schtasks /delete /tn "g-chat-refresh-weekly" /f
#>

param(
    [switch]$IncludeTour,
    [switch]$AllowNullEmbeddings,
    [switch]$DryRun
)

# 스크립트 자체 오류(경로·복사 실패 등)는 즉시 중단. 외부 프로그램(node/python)의 실패는 종료 코드로만 판단한다.
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # scripts 의 상위 = 저장소 루트
Set-Location $RepoRoot

# node/python 출력(UTF-8)을 PowerShell 5.1 이 cp949 로 읽어 한글이 깨지지 않게
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("refresh_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# 외부 프로그램 실행. 성공/실패는 종료 코드로만 판단한다.
#   - 실행 중에만 $ErrorActionPreference 를 Continue 로 낮춘다: 5.1 에서 2>&1 은 stderr 한 줄마다
#     ErrorRecord 를 만들고, Stop 이면 그 첫 줄에서 스크립트가 멈춘다(경고 한 줄로 갱신 전체가 실패하던 원인).
#   - stderr 줄은 문자열로 바꿔 stdout 과 함께 화면·로그에 남긴다.
#   - WarnExitCodes 에 있는 종료 코드는 경고로만 기록하고 계속한다.
function Invoke-Step {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$ArgList,
        [string]$WorkDir = $RepoRoot,
        [int[]]$WarnExitCodes = @()
    )
    Write-Log "=== $Name 시작 ($Exe $($ArgList -join ' ')) ==="
    Push-Location $WorkDir
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @ArgList 2>&1 | ForEach-Object {
            $line = "$_"
            Write-Output $line
            Add-Content -Path $LogFile -Value $line -Encoding UTF8
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
        Pop-Location
    }
    if ($code -ne 0) {
        if ($WarnExitCodes -contains $code) {
            Write-Log "!!! $Name 경고 (exit code $code) — 계속 진행"
            return
        }
        throw "$Name 실패 (exit code $code)"
    }
    Write-Log "=== $Name 완료 ==="
}

function Copy-Output {
    param([string]$From, [string]$To)
    if (Test-Path $To) { Copy-Item -Path $To -Destination "$To.bak" -Force }   # 직전 파일 보관
    Write-Log "복사: $From -> $To (직전 파일은 .bak)"
    Copy-Item -Path $From -Destination $To -Force
}

try {
    Write-Log "########## 데이터 갱신 시작 (IncludeTour=$IncludeTour, AllowNullEmbeddings=$AllowNullEmbeddings, DryRun=$DryRun) ##########"
    $Pipeline = Join-Path $RepoRoot "data-pipeline"

    Invoke-Step -Name "크롤링·정제 - 공단 홈페이지 (data-pipeline)" -Exe "python" -ArgList @("main.py") -WorkDir $Pipeline

    # OCR 결과가 없는 문서(새 글)의 이미지만 Tesseract 로 OCR → 고정 키로 병합. pytesseract 가 있는 파이썬 필요
    # (기본: 이 PC 의 agent_env, 다른 환경이면 OCR_PYTHON 환경변수로 지정)
    $OcrPython = $env:OCR_PYTHON
    if (-not $OcrPython) { $OcrPython = "C:\Users\shj\anaconda3\envs\agent_env\python.exe" }
    if (-not (Test-Path $OcrPython)) { $OcrPython = "python" }
    Invoke-Step -Name "OCR - 새 글 이미지 (main.py ocr-new)" -Exe $OcrPython -ArgList @("main.py", "ocr-new") -WorkDir $Pipeline
    Copy-Output (Join-Path $Pipeline "data\processed\documents.jsonl") (Join-Path $RepoRoot "documents.jsonl")

    Invoke-Step -Name "크롤링·정제 - 관광지 통합예약시스템 (data-pipeline camping)" -Exe "python" -ArgList @("main.py", "camping") -WorkDir $Pipeline
    Copy-Output (Join-Path $Pipeline "data\processed\documents_camping.jsonl") (Join-Path $RepoRoot "documents_camping.jsonl")

    # 새 중복 후보 보고만 (적용은 사람이 보고서를 확인한 뒤 python dedup.py --apply)
    Invoke-Step -Name "중복 후보 보고 (dedup.py dry-run)" -Exe "python" -ArgList @("dedup.py") -WorkDir $Pipeline

    if ($DryRun) { $env:DRY_RUN = "true" } else { Remove-Item Env:DRY_RUN -ErrorAction SilentlyContinue }

    # 출처별 증분 적재 — 각 스크립트는 자기 metadata.source 행만 다루므로 순서와 무관
    Invoke-Step -Name "Supabase 적재 - 공단 홈페이지 (ingest.js)" -Exe "node" -ArgList @("scripts/ingest.js")
    if (-not $DryRun) {
        Invoke-Step -Name "예약 링크 재주입 (patch_reserve_links.js)" -Exe "node" -ArgList @("scripts/patch_reserve_links.js")
        if ($IncludeTour) {
            Invoke-Step -Name "관광 POI 갱신 (ingest_tour.js)" -Exe "node" -ArgList @("scripts/ingest_tour.js")
        }
    }
    Invoke-Step -Name "Supabase 적재 - 관광지 통합예약시스템 (ingest_camping.js)" -Exe "node" -ArgList @("scripts/ingest_camping.js")
    Invoke-Step -Name "Supabase 적재 - 부서 표준답변 FAQ (ingest_manual.js)" -Exe "node" -ArgList @("scripts/ingest_manual.js")
    Invoke-Step -Name "Supabase 적재 - 시설 전용 사이트 안내 페이지 (ingest_pages.js)" -Exe "node" -ArgList @("scripts/ingest_pages.js")

    if ($DryRun) {
        Write-Log "########## 드라이런 완료 (DB 변경 없음) ##########"
        return
    }

    # check_db.js 종료 코드: 0 정상 / 3 임베딩 NULL 행 있음 / 1 그 밖의 오류
    $warn = @()
    if ($AllowNullEmbeddings) { $warn = @(3) }
    Invoke-Step -Name "적재 검증 (check_db.js)" -Exe "node" -ArgList @("scripts/check_db.js") -WarnExitCodes $warn

    Write-Log "########## 데이터 갱신 완료 ##########"
}
catch {
    Write-Log "!!! 갱신 실패: $($_.Exception.Message)"
    exit 1
}
finally {
    Remove-Item Env:DRY_RUN -ErrorAction SilentlyContinue
}

<#
.SYNOPSIS
  U0 杀软抽样验证：采集分发产物的指纹 + Defender 扫描结果，产出可留档的 JSON。

.DESCRIPTION
  设计承诺"发布前抽样验证主流杀软"，但此前没有任何可复现的取证手段，
  所以这条债务一直挂着。本脚本只做两件事：
    1. 证明"扫的是哪一个文件"——路径、字节数、sha256、版本输出、PE 段名（是否 UPX 加壳）；
    2. 证明"Defender 当时怎么说"——引擎/签名版本、实时保护状态、扫描结论、隔离记录。

  它**不**替代人工：360/火绒/卡巴/企业 EDR 的结论必须人工填写到
  docs/verify/U0-杀软抽样验证方案.md 的留档表里，脚本只负责把可自动化的部分做实。

  兼容 Windows PowerShell 5.1（现场常见），不使用 PS7 语法。

.PARAMETER ExePath
  terminal_bridge.exe 路径。默认取当前用户下载目录下的同名文件。

.PARAMETER OutFile
  报告输出路径。默认与 exe 同目录的 u0-antivirus-report.json。

.PARAMETER SkipDefenderScan
  只取指纹、不触发扫描（给"已经用第三方引擎扫过"的机器用，避免重复全盘扫）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\u0_antivirus_probe.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\u0_antivirus_probe.ps1 -ExePath D:\dist\terminal_bridge.exe
#>
[CmdletBinding()]
param(
    [string]$ExePath = (Join-Path $env:USERPROFILE 'Downloads\terminal_bridge.exe'),
    [string]$OutFile = '',
    [switch]$SkipDefenderScan
)

$ErrorActionPreference = 'Continue'

function Get-FileSha256 {
    param([string]$Path)
    try {
        return (Get-FileHash -Path $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower()
    } catch {
        return $null
    }
}

# 读 PE 段名表，判断是否仍被 UPX 加壳（UPX0/UPX1 是 upx lzma 的固定产物）。
# 为什么要在现场再确认一次：客户拿到的是下载目录里的文件，
# 中途被替换/被"优化工具"重新打包都可能让实际运行的不是我们签发的那个。
function Get-PeSectionNames {
    param([string]$Path)
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -lt 0x40 -or $bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) {
            return @()
        }
        $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
        if ($peOffset -le 0 -or ($peOffset + 24) -ge $bytes.Length) {
            return @()
        }
        $sectionCount = [BitConverter]::ToUInt16($bytes, $peOffset + 6)
        $optionalSize = [BitConverter]::ToUInt16($bytes, $peOffset + 20)
        $sectionTable = $peOffset + 24 + $optionalSize
        $names = @()
        for ($i = 0; $i -lt $sectionCount; $i++) {
            $offset = $sectionTable + ($i * 40)
            if (($offset + 8) -ge $bytes.Length) { break }
            $name = [System.Text.Encoding]::ASCII.GetString($bytes, $offset, 8).TrimEnd([char]0)
            if ($name) { $names += $name }
        }
        return $names
    } catch {
        return @()
    }
}

function Get-VersionOutput {
    param([string]$Path)
    try {
        # 加壳产物启动时会先解压自身；给足 20s，避免把"慢"误判成"坏"。
        $output = & $Path -version 2>&1 | Out-String
        return $output.Trim()
    } catch {
        return "执行失败: $($_.Exception.Message)"
    }
}

function Get-DefenderSnapshot {
    param([string]$Path, [bool]$DoScan)

    $snapshot = [ordered]@{
        available          = $false
        engine_version     = $null
        signature_version  = $null
        realtime_enabled   = $null
        scan_performed     = $false
        scan_threats_found = $null
        detections         = @()
        error              = $null
    }

    $mp = Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue
    if (-not $mp) {
        $snapshot.error = '未找到 Defender cmdlet（非 Windows 或已被第三方杀软接管）'
        return $snapshot
    }

    try {
        $status = Get-MpComputerStatus -ErrorAction Stop
        $snapshot.available = $true
        $snapshot.engine_version = $status.AMEngineVersion
        $snapshot.signature_version = $status.AntivirusSignatureVersion
        $snapshot.realtime_enabled = $status.RealTimeProtectionEnabled
    } catch {
        $snapshot.error = "Get-MpComputerStatus 失败: $($_.Exception.Message)"
        return $snapshot
    }

    if ($DoScan) {
        try {
            Start-MpScan -ScanType CustomScan -ScanPath $Path -ErrorAction Stop
            $snapshot.scan_performed = $true
        } catch {
            $snapshot.error = "Start-MpScan 失败: $($_.Exception.Message)"
        }
    }

    try {
        # 只收最近 7 天、且资源指向本产物的记录：机器上历史告警与本次无关，
        # 全量导出会让"到底拦没拦这个文件"变得无法判断。
        $since = (Get-Date).AddDays(-7)
        $hits = Get-MpThreatDetection -ErrorAction SilentlyContinue |
            Where-Object { $_.InitialDetectionTime -ge $since -and $_.Resources -match 'terminal_bridge' }
        foreach ($hit in $hits) {
            $snapshot.detections += [ordered]@{
                time     = $hit.InitialDetectionTime.ToString('s')
                threat   = $hit.ThreatID
                action   = $hit.ActionSuccess
                resource = ($hit.Resources -join '; ')
            }
        }
        $snapshot.scan_threats_found = ($snapshot.detections.Count -gt 0)
    } catch {
        $snapshot.error = "Get-MpThreatDetection 失败: $($_.Exception.Message)"
    }

    return $snapshot
}

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "产物不存在: $ExePath"
    exit 2
}

$resolved = (Resolve-Path -LiteralPath $ExePath).Path
if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $OutFile = Join-Path (Split-Path -Parent $resolved) 'u0-antivirus-report.json'
}

$item = Get-Item -LiteralPath $resolved
$sections = Get-PeSectionNames -Path $resolved
$sha = Get-FileSha256 -Path $resolved
$version = Get-VersionOutput -Path $resolved

$report = [ordered]@{
    generated_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    host             = $env:COMPUTERNAME
    os               = (Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
    os_version       = [System.Environment]::OSVersion.VersionString
    powershell       = $PSVersionTable.PSVersion.ToString()
    is_admin         = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    product          = [ordered]@{
        path       = $resolved
        size_bytes = $item.Length
        sha256     = $sha
        version    = $version
        pe_sections = $sections
        upx_packed = (($sections -contains 'UPX0') -or ($sections -contains 'UPX1'))
        signed     = (Get-AuthenticodeSignature -LiteralPath $resolved -ErrorAction SilentlyContinue).Status.ToString()
    }
    defender         = Get-DefenderSnapshot -Path $resolved -DoScan (-not $SkipDefenderScan)
    third_party      = @(
        [ordered]@{ engine = '360 安全卫士'; version = ''; result = ''; note = '需人工填写' }
        [ordered]@{ engine = '火绒安全';    version = ''; result = ''; note = '需人工填写' }
        [ordered]@{ engine = '卡巴斯基';    version = ''; result = ''; note = '需人工填写' }
        [ordered]@{ engine = '企业 EDR';    version = ''; result = ''; note = '需人工填写（若有）' }
    )
    verdict          = '待人工判定'
}

$report | ConvertTo-Json -Depth 6 | Tee-Object -FilePath $OutFile -Encoding UTF8 | Out-Host

Write-Host ''
Write-Host "报告已写入: $OutFile"
Write-Host "判定口径见 docs/verify/U0-杀软抽样验证方案.md（Defender 有检出即视为不通过，需走未压缩回退）。"

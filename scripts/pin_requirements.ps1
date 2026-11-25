# 从「当前 Python 环境」生成逐版本锁定文件 requirements.lock.txt
#
# 为什么要它：requirement.txt 只有版本边界（>=x,<y），每次装到的具体版本会漂；
# 而 P0 #4 的页码链路、D7-a 的切块行为都依赖 docling 的导出细节，
# docling 一漂就可能变。lock 文件把当前能跑的版本组合钉死。
#
# 用法（在仓库根，PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\pin_requirements.ps1
#   指定解释器：
#   powershell -ExecutionPolicy Bypass -File scripts\pin_requirements.ps1 -Python "D:\path\to\python.exe"
#
# 生成后请提交 requirements.lock.txt；部署时装它而不是 requirement.txt：
#   python -m pip install -r requirements.lock.txt

param(
    [string]$Python = "python",
    [string]$Out = "requirements.lock.txt"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$OutPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $repoRoot $Out }

$ver = & $Python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "找不到可用的 Python 解释器（$Python）。用 -Python 指定完整路径。" }

$freeze = & $Python -m pip freeze 2>&1
if ($LASTEXITCODE -ne 0) { throw "pip freeze 失败：$freeze" }

$lines = @(
    "# 由 scripts/pin_requirements.ps1 生成的逐版本锁定文件（请勿手改）",
    "# 生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "# 来源环境: $ver",
    "# 复现: python -m pip install -r requirements.lock.txt"
) + @($freeze)

Set-Content -Path $OutPath -Value $lines -Encoding UTF8
Write-Host "已写入 $OutPath（$(@($freeze).Count) 个包，来源 $ver）"
Write-Host "复现环境: python -m pip install -r $OutPath"
Write-Host "装完建议顺手做一次语法自检: python -m compileall src"

# pip freeze 偶尔会写进机器相关的行（如 `-e git+...`、`pkg @ file:///D:/...`），
# 换机器装会失败 —— 下面这些行需要人工看一跟。
$odd = @($freeze | Where-Object { $_ -match '^(\s*-e\s)|@\s*file:|^\s*#.*\begg\b' })
if ($odd.Count -gt 0) {
    Write-Host ""
    Write-Host "[注意] lock 里有机器相关行，换机器前请人工确认："
    $odd | ForEach-Object { Write-Host "  $_" }
}

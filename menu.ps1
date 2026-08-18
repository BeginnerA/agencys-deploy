# 智能运维平台交互式发布菜单入口（Windows PowerShell 版）
# 用法: .\menu.ps1

$python = $null
foreach ($name in @("python", "python3")) {
    if (Get-Command $name -ErrorAction SilentlyContinue) {
        $python = $name
        break
    }
}
if (-not $python) {
    Write-Host "[platform] 未找到 Python，请先安装 Python 3 并加入 PATH" -ForegroundColor Red
    exit 1
}

# 依赖预检（与 deploy.ps1 保持一致）
$check = & $python -c "import yaml, requests, pymysql, paramiko" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[platform] 缺少 Python 依赖，请先执行: pip install -r requirements.txt" -ForegroundColor Red
    Write-Host $check
    exit 1
}

$menuPy = Join-Path $PSScriptRoot "menu.py"
& $python $menuPy
exit $LASTEXITCODE

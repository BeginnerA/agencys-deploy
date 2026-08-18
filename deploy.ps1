# 智能运维平台单服务发布入口脚本（Windows PowerShell 版）
# 与 deploy.sh 功能等价，参数完全一致，直接透传给 deploy.py 解析。
#
# 用法:
#   .\deploy.ps1 --project xxx --version 1.0.29 --previous-version 1.0.28 --env test
#   .\deploy.ps1 -h    # 查看 deploy.py 的完整参数说明
#
# 设计说明:
#   - 本脚本只做 Python 依赖预检和参数透传，所有参数解析/校验由 deploy.py 负责，
#     因此 deploy.py 新增参数时无需同步修改本脚本（deploy.py 是唯一参数权威）。
#   - Maven/JDK 定位不在此重复实现：build.py 按三级顺序查找
#     （config/deploy*.yaml 的 build.maven_home/java_home → 环境变量/PATH → 常见位置），
#     入口脚本不做硬编码，避免多份逻辑漂移。
#   - 需要 bash 环境（Linux/macOS/WSL/Git Bash）时请使用 deploy.sh。

# 定位 Python 解释器（优先 python，回退 python3）
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

# 依赖预检（与 deploy.sh 保持一致）
$check = & $python -c "import yaml, requests, pymysql, paramiko" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[platform] 缺少 Python 依赖，请先执行: pip install -r requirements.txt" -ForegroundColor Red
    Write-Host $check
    exit 1
}

$deployPy = Join-Path $PSScriptRoot "deploy.py"
& $python $deployPy @args
exit $LASTEXITCODE

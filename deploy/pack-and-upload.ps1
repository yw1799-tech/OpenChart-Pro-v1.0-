# OpenChart Pro 本地打包 + 上传到生产服务器（PowerShell 脚本）
# 用法：在项目根目录右键 PowerShell 执行：
#   powershell -ExecutionPolicy Bypass -File deploy\pack-and-upload.ps1
#
# 前提：Windows 10/11 自带 OpenSSH（包含 ssh + scp 命令）

$ErrorActionPreference = "Stop"
$ServerIP = "43.165.185.243"
$ServerUser = "ubuntu"
$ProjectRoot = (Get-Item -Path "..").FullName
if ($MyInvocation.MyCommand.Path) {
    $ProjectRoot = (Get-Item (Split-Path -Parent $MyInvocation.MyCommand.Path)).Parent.FullName
}

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  OpenChart Pro 部署包打包 + 上传" -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  项目根: $ProjectRoot"
Write-Host "  目标:   $ServerUser@${ServerIP}"
Write-Host ""

# 1. 用 tar 打包（Win10+ 自带 tar）
$PkgFile = Join-Path $env:TEMP "openchart-pkg.tar.gz"
Write-Host "[1/3] 打包项目（排除 __pycache__/.git/logs/截图）..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    # tar 打包（包含 backend / frontend / data / requirements.txt / run.py / .env.example / deploy / docs）
    & tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' `
          --exclude='logs/*' --exclude='*.png' --exclude='node_modules' `
          --exclude='archive' --exclude='debug_*.py' --exclude='diag_*.py' `
          --exclude='*.tar.gz' `
          -czf $PkgFile .
    if ($LASTEXITCODE -ne 0) { throw "tar 打包失败" }
    $size = [math]::Round((Get-Item $PkgFile).Length / 1MB, 1)
    Write-Host "  ✓ 打包成功: $PkgFile ($size MB)" -ForegroundColor Green
} finally {
    Pop-Location
}

# 2. scp 上传到服务器 ~/openchart-pkg.tar.gz
Write-Host ""
Write-Host "[2/3] 上传到服务器（输入密码 XU8V7;Ln+-KGC(e）..." -ForegroundColor Yellow
& scp -o StrictHostKeyChecking=no $PkgFile "${ServerUser}@${ServerIP}:~/openchart-pkg.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "scp 失败" }
Write-Host "  ✓ 上传成功" -ForegroundColor Green

# 3. 远程解压 + 跑 setup.sh
Write-Host ""
Write-Host "[3/3] 远程解压 + 一键安装（再次输入密码）..." -ForegroundColor Yellow
$RemoteCmd = @"
set -e
mkdir -p ~/openchart-pkg
tar -xzf ~/openchart-pkg.tar.gz -C ~/openchart-pkg
sudo bash ~/openchart-pkg/deploy/setup.sh
"@
& ssh -o StrictHostKeyChecking=no "${ServerUser}@${ServerIP}" $RemoteCmd
if ($LASTEXITCODE -ne 0) { throw "ssh setup 失败" }

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  ✅ 部署完成！" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  访问: http://${ServerIP}:8000" -ForegroundColor Green
Write-Host ""
Write-Host "  下一步：" -ForegroundColor Yellow
Write-Host "  1) ssh ${ServerUser}@${ServerIP}" -ForegroundColor White
Write-Host "  2) sudo vim /opt/openchart/.env  填入 DEEPSEEK_API_KEY" -ForegroundColor White
Write-Host "  3) sudo systemctl restart openchart" -ForegroundColor White
Write-Host "  4) journalctl -u openchart -f  看实时日志" -ForegroundColor White

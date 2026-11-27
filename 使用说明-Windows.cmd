@echo off
REM ============================================================
REM  SigninHub iOS - 一键出包（Windows 上只需推送，编译在 GitHub 跑）
REM
REM  本机没有 macOS/Xcode 时，用 GitHub Actions 出 ipa：
REM    1. 把本目录推到你的 GitHub 仓库
REM    2. 打开仓库 Actions -> Build unsigned IPA -> Run workflow
REM    3. 下载 Artifact，解压得到 SigninHub.ipa
REM    4. 传到 iPhone，用 TrollStore 安装
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo  SigninHub iOS - TrollStore 无签名 ipa
echo ============================================================
echo.
echo  本目录是一个完整可构建的 iOS 工程。
echo.
echo  路线 A（推荐，无需 Mac）: 用 GitHub Actions
echo    - 推到 GitHub 后，在 Actions 里点 Run workflow
echo    - 产物在 Artifacts: SigninHub-unsigned-ipa
echo.
echo  路线 B（有 Mac）: 在本机跑
echo    ./scripts/fetch_python.sh
echo    ./scripts/prepare_app.sh
echo    ./scripts/build_unsigned_ipa.sh
echo.
echo  安装: 把 .ipa 传给 iPhone，TrollStore 打开 -^> Install
echo.
echo  详细说明见 README.md
echo ============================================================
echo.
pause

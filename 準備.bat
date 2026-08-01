@echo off
rem 初回だけ実行する準備スクリプト。ダブルクリックで動く。
chcp 65001 > nul
cd /d "%~dp0"

set "FFURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

echo.
echo spl-clip の準備を始めます(初回だけ・数分かかります)
echo.

rem ---- 1. Python を探す ----------------------------------------------------
set "PYOK="
for /f %%v in ('python -c "import sys;print(1 if sys.version_info>=(3,10) else 0)" 2^>nul') do set "PYOK=%%v"
if not "%PYOK%"=="1" goto nopython
for /f "delims=" %%v in ('python -V 2^>^&1') do echo   使う Python: %%v

rem ---- 2. 専用の環境を作る -------------------------------------------------
echo.
echo 1/3 専用の環境を作っています...
if exist "venv\Scripts\python.exe" (
  echo   すでにあるものを使います
) else (
  python -m venv venv
  if errorlevel 1 goto venvfail
)

rem ---- 3. ライブラリを入れる -----------------------------------------------
echo.
echo 2/3 必要なライブラリを入れています(数分かかることがあります)...
venv\Scripts\python.exe -m pip install --quiet --upgrade pip 2>nul
venv\Scripts\python.exe -m pip install --quiet numpy streamlit
if errorlevel 1 goto pipfail
echo   完了

rem ---- 4. ffmpeg を用意する ------------------------------------------------
echo.
echo 3/3 動画処理ツール(ffmpeg)を用意しています...
if exist "bin\ffmpeg.exe" if exist "bin\ffprobe.exe" goto ffdone
where ffmpeg >nul 2>&1
if not errorlevel 1 (
  where ffprobe >nul 2>&1
  if not errorlevel 1 (
    echo   パソコンに入っているものを使います
    goto ffdone
  )
)

if not exist bin mkdir bin
echo   ダウンロードしています(110MBほどあります)...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%FFURL%' -OutFile 'bin\ff.zip' } catch { exit 1 }"
if errorlevel 1 goto ffFail
echo   展開しています...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path 'bin\ff.zip' -DestinationPath 'bin\tmp' -Force; Get-ChildItem -Path 'bin\tmp' -Recurse -Include ffmpeg.exe,ffprobe.exe | Copy-Item -Destination 'bin' -Force; Remove-Item -Recurse -Force 'bin\tmp','bin\ff.zip'"
if not exist "bin\ffmpeg.exe" goto ffFail
echo   完了

:ffdone
echo.
echo 準備ができました!
echo これからは「起動.bat」をダブルクリックすれば使えます。
echo.
pause
exit /b 0

:nopython
echo Python 3.10 以降が見つかりませんでした。
echo.
where winget >nul 2>&1
if errorlevel 1 goto manualpython
echo winget を使って自動でインストールできます。
set /p "ANS=インストールしますか? (y/n) "
if /i not "%ANS%"=="y" goto manualpython
winget install -e --id Python.Python.3.12
echo.
echo インストールが終わったら、この「準備.bat」をもう一度ダブルクリックしてください。
echo.
pause
exit /b 1

:manualpython
echo https://www.python.org/downloads/ からインストールしてください。
echo インストール時に「Add python.exe to PATH」に必ずチェックを入れてください。
echo そのあと、この「準備.bat」をもう一度ダブルクリックしてください。
echo.
pause
exit /b 1

:venvfail
echo 環境の作成に失敗しました。
echo.
pause
exit /b 1

:pipfail
echo ライブラリの導入に失敗しました。インターネット接続を確認してください。
echo.
pause
exit /b 1

:ffFail
echo ffmpeg の用意に失敗しました。インターネット接続を確認してください。
echo.
pause
exit /b 1

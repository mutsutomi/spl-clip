@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist "venv\Scripts\streamlit.exe" goto nosetup

echo 操作画面を準備しています。ブラウザが自動で開きます...
echo 終わるときは、このウィンドウで Ctrl + C を押すか、ウィンドウを閉じてください。
echo.

rem サーバーが立ち上がるのを待ってからブラウザを開く(裏で待機させる)
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$u='http://localhost:8501'; for($i=0; $i -lt 60; $i++){ try { $c = New-Object Net.Sockets.TcpClient('localhost', 8501); $c.Close(); Start-Process $u; break } catch { Start-Sleep -Milliseconds 500 } }"

venv\Scripts\streamlit.exe run app.py
goto end

:nosetup
echo まだ準備ができていません。
echo 同じフォルダにある「準備.bat」を先にダブルクリックしてください。
echo.
pause

:end

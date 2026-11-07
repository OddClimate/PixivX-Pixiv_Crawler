@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name PixivX --icon pixiv.ico --add-data "pixiv.ico;." --collect-all ttkbootstrap --hidden-import socks pixiv_client.py
if errorlevel 1 goto fail
copy /y "dist\PixivX.exe" "PixivX.exe" >nul
rmdir /s /q build
rmdir /s /q dist
echo.
echo Build done: %~dp0PixivX.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1

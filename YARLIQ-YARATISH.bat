@echo off
setlocal
cd /d "%~dp0"

set QUIET=%1
if not "%QUIET%"=="quiet" (
    echo.
    echo   Ish stolida yorliq yaratilmoqda...
    echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desk=[Environment]::GetFolderPath('Desktop');" ^
  "$lnk=Join-Path $desk 'Sevimli Kassa - Panel.lnk';" ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
  "$s.TargetPath='%~dp0PANEL.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%~dp0panel.ico';" ^
  "$s.Description='Sevimli Kassa - boshqaruv paneli';" ^
  "$s.Save();" ^
  "if (Test-Path $lnk) { Write-Host ('  Tayyor: ' + $lnk) }"

if "%QUIET%"=="quiet" exit /b 0
echo.
pause

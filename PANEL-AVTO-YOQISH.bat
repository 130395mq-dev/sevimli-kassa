@echo off
setlocal
cd /d "%~dp0"

echo.
echo   Panel kompyuter yoqilganda o'zi ochiladigan bo'ladi.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir=[Environment]::GetFolderPath('Startup');" ^
  "$lnk=Join-Path $dir 'Sevimli Kassa - Panel.lnk';" ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
  "$s.TargetPath='%~dp0PANEL.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%~dp0panel.ico';" ^
  "$s.Description='Sevimli Kassa - panel';" ^
  "$s.Save();" ^
  "if (Test-Path $lnk) { Write-Host ('  Yoqildi.') }"

echo.
pause

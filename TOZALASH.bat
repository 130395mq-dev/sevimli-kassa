@echo off
chcp 65001 >nul
title Eski ERP fayllarini tozalash
cd /d "%~dp0"

echo.
echo ============================================================
echo   ESKI ERP FAYLLARINI TOZALASH
echo ============================================================
echo.
echo   Bu papkadan quyidagilar o'chiriladi:
echo.
echo     erp\                                       (butun papka)
echo     dashboard\erp_views.py
echo     dashboard\test_erp.py
echo     dashboard\templates\dashboard\erp_export.html
echo.
echo   Bular Sevimli Kassa'ga kerak emas.
echo   ERP endi butunlay alohida loyiha.
echo.
echo   Papka: %CD%
echo.
echo ------------------------------------------------------------
echo.

if not exist "manage.py" (
  echo   XATO: bu papkada manage.py yo'q.
  echo   Fayl noto'g'ri joyda turibdi - uni Kassa papkasiga ko'chiring.
  echo.
  pause
  exit /b 1
)

choice /c HY /n /m "  O'chirilsinmi?  [H] Ha   [Y] Yo'q : "
if errorlevel 2 goto BEKOR

echo.
if exist "erp" ( rmdir /s /q "erp" && echo   [o'chdi] erp\ ) else echo   [yo'q edi] erp\
if exist "dashboard\erp_views.py" ( del /q "dashboard\erp_views.py" && echo   [o'chdi] dashboard\erp_views.py ) else echo   [yo'q edi] dashboard\erp_views.py
if exist "dashboard\test_erp.py" ( del /q "dashboard\test_erp.py" && echo   [o'chdi] dashboard\test_erp.py ) else echo   [yo'q edi] dashboard\test_erp.py
if exist "dashboard\templates\dashboard\erp_export.html" ( del /q "dashboard\templates\dashboard\erp_export.html" && echo   [o'chdi] erp_export.html ) else echo   [yo'q edi] erp_export.html

echo.
echo ------------------------------------------------------------
echo   TAYYOR. Endi Kassa dasturini ishga tushirib ko'ring.
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:BEKOR
echo.
echo   Bekor qilindi - hech narsa o'chirilmadi.
echo.
pause
exit /b 0

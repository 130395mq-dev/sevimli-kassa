@echo off
setlocal enabledelayedexpansion
title Sevimli Kassa - Panel
cd /d "%~dp0"

echo.
echo   SEVIMLI KASSA - BOSHQARUV PANELI
echo   versiya 1.0.0
echo   ================================
echo.

set PY=
for %%V in (3.12 3.11 3.13 3.10) do (
    if not defined PY (
        py -%%V --version >nul 2>&1 && set PY=py -%%V
    )
)
if not defined PY (
    py -3 --version >nul 2>&1 && set PY=py -3
)
if not defined PY (
    python --version >nul 2>&1 && set PY=python
)

if not defined PY (
    echo   PYTHON TOPILMADI
    echo.
    echo   Microsoft Store ni oching, qidiruvga "Python 3.12" yozing
    echo   va o'rnating. Keyin bu faylni qayta bosing.
    echo.
    pause
    exit /b 1
)

echo   Python:
%PY% --version
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   Birinchi ishga tushirish - kutubxonalar yuklanmoqda.
    echo   Bu 1-2 daqiqa oladi, faqat bir marta.
    echo.
    %PY% -m venv .venv
    if errorlevel 1 goto fail
)

set VPY=.venv\Scripts\python.exe

%VPY% -c "import django" >nul 2>&1
if errorlevel 1 (
    %VPY% -m pip install --upgrade pip --quiet --disable-pip-version-check
    %VPY% -m pip install -r requirements-local.txt --disable-pip-version-check
    if errorlevel 1 goto fail
    echo.
    echo   Kutubxonalar o'rnatildi.
    echo.
)

call "%~dp0YARLIQ-YARATISH.bat" quiet

%VPY% -m tools.panel
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo   XATOLIK. Yuqoridagi yozuvni suratga olib yuboring.
echo.
pause
exit /b 1

@echo off
setlocal
title Sevimli Kassa - GitHub yuklash
cd /d "%~dp0"

echo.
echo   SEVIMLI KASSA - kodni GitHub ga yuklash
echo   ========================================
echo.

git --version >nul 2>&1
if errorlevel 1 (
    echo   GIT TOPILMADI.
    echo   git-scm.com/download/win dan ornating, keyin qayta bosing.
    echo.
    pause
    exit /b 1
)

if not exist ".git" git init
git config user.email "deploy@sevimli.uz"
git config user.name "Sevimli Deploy"
git add -A
git commit -m "Sevimli Kassa server" >nul 2>&1
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin https://github.com/130395mq-dev/sevimli-kassa.git
echo   Yuklanmoqda... (GitHub parol/kirish sorashi mumkin)
echo.
git push -u origin main --force
if errorlevel 1 (
    echo.
    echo   YUKLANMADI. Yuqoridagi yozuvni suratga oling.
    echo.
    pause
    exit /b 1
)
echo.
echo   TAYYOR - kod GitHub ga yuklandi.
echo.
pause
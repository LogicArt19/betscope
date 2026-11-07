@echo off
chcp 65001 >nul
title BetScope - fabrication du .exe
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python n'est pas installe. Installe-le depuis https://www.python.org/downloads/
  echo  ^(coche bien "Add python.exe to PATH" pendant l'installation^)
  pause
  exit /b 1
)
echo [1/2] Installation de PyInstaller...
%PY% -m pip install --upgrade pyinstaller pywebview || goto :err
echo [2/2] Fabrication de BetScope.exe...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name Cotescope --icon assets\betscope.ico --add-data "ui;ui" --add-data "assets;assets" betscope.py || goto :err
echo.
echo ===============================================
echo  Termine ! Ton logiciel : dist\BetScope.exe
echo ===============================================
explorer dist
pause
exit /b 0
:err
echo Une erreur est survenue.
pause
exit /b 1

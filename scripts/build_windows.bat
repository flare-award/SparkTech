# Сборка SparkTech на Windows через командную строку (без PowerShell-скрипта).
# Использование:  build_windows.bat
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
"
echo === SparkTech: сборка Windows .exe ===
where python >nul 2>nul || (echo Python не найден. Установите Python 3.10+ и повторите. & exit /b 1)

if not exist ".venv\Scripts\python.exe" (
    echo Создание виртуального окружения...
    python -m venv .venv || exit /b 1
)

set PY=.venv\Scripts\python.exe
rem UTF-8 для сообщений Python, чтобы кириллица в консоли читалась
set PYTHONUTF8=1
echo Установка зависимостей...
%PY% -m pip install --upgrade pip >nul || exit /b 1
%PY% -m pip install -r requirements-dev.txt pyinstaller pywebview || exit /b 1

echo Тесты...
%PY% -m pytest -q || (echo Тесты не прошли — сборка остановлена. & exit /b 1)

if exist "vendor\platform-tools\adb.exe" (
    echo adb найден в vendor\platform-tools — будет включён в сборку.
) else (
    echo ВНИМАНИЕ: vendor\platform-tools\adb.exe нет. Сборка будет без adb.
    echo Скачать официально: https://developer.android.com/tools/releases/platform-tools
)

echo Сборка...
%PY% -m PyInstaller SparkTech.spec --noconfirm --clean || exit /b 1

if exist "dist\SparkTech.exe" (
    echo.
    echo Готово: dist\SparkTech.exe
) else (
    echo Ошибка: dist\SparkTech.exe не создан.
    exit /b 1
)
endlocal

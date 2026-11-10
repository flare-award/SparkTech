<#
.SYNOPSIS
    Сборка SparkTech.exe для Windows.

.DESCRIPTION
    Скрипт создаёт виртуальное окружение, ставит зависимости, прогоняет тесты
    и собирает один исполняемый файл dist\SparkTech.exe.

    Официальный adb (Android SDK Platform-Tools) НЕ скачивается автоматически.
    Если вы хотите включить его в сборку, скачайте архив вручную с
    https://developer.android.com/tools/releases/platform-tools и распакуйте
    platform-tools в каталог vendor\platform-tools. Скрипт проверит наличие
    adb.exe и покажет, что именно попадёт в сборку.

.PARAMETER SkipTests
    Не запускать тесты перед сборкой (не рекомендуется).

.PARAMETER Clean
    Удалить build\ и dist\ перед сборкой.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 по умолчанию пишет в OEM-кодировке, из-за чего
# русские сообщения превращаются в мусор. Переключаем консоль на UTF-8.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }

if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Write-Warning "Скрипт рассчитан на Windows. Сборка .exe возможна только на Windows."
}

Write-Step "Проверка Python"
$python = "python"
try { & $python --version } catch {
    throw "Python не найден. Установите Python 3.10+ с python.org и включите 'Add python.exe to PATH'."
}

Write-Step "Виртуальное окружение .venv"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $python -m venv .venv
}
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

Write-Step "Установка зависимостей"
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -r requirements-dev.txt pyinstaller pywebview

if ($Clean) {
    Write-Step "Очистка build и dist"
    Remove-Item -Recurse -Force build\SparkTech, dist -ErrorAction SilentlyContinue
}

if (-not $SkipTests) {
    Write-Step "Тесты"
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Тесты не прошли — сборка остановлена." }
}

Write-Step "Проверка Android SDK Platform-Tools"
$vendorAdb = "vendor\platform-tools\adb.exe"
if (Test-Path $vendorAdb) {
    Write-Host "adb.exe найден: $vendorAdb — будет включён в сборку." -ForegroundColor Green
    Write-Host "Не забудьте: Platform-Tools распространяются Google по Android SDK License Agreement." -ForegroundColor DarkYellow
} else {
    Write-Warning "vendor\platform-tools\adb.exe не найден."
    Write-Warning "exe будет собран без adb; пользователь укажет путь к официальным Platform-Tools в настройках."
    Write-Warning "Скачать вручную: https://developer.android.com/tools/releases/platform-tools"
}

Write-Step "Сборка exe"
& $venvPython -m PyInstaller SparkTech.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой." }

$exe = "dist\SparkTech.exe"
if (-not (Test-Path $exe)) { throw "Ожидаемый файл $exe не создан." }

Write-Step "Готово"
$size = (Get-Item $exe).Length / 1MB
Write-Host ("Собран {0} ({1:N1} МБ)" -f (Resolve-Path $exe), $size) -ForegroundColor Green
Write-Host "Запуск: .\dist\SparkTech.exe"
Write-Host "Помощь: README.md, раздел «Сборка Windows .exe»."

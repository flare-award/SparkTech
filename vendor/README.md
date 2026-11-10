# vendor/platform-tools

Сюда можно **вручную** положить официальные Android SDK Platform-Tools,
если вы хотите, чтобы `adb.exe` попал в сборку `SparkTech.exe`.

1. Скачайте архив **только** с официальной страницы:
   https://developer.android.com/tools/releases/platform-tools
2. Распакуйте так, чтобы получилось:

```
vendor/platform-tools/adb.exe
vendor/platform-tools/AdbWinApi.dll
vendor/platform-tools/AdbWinUsbApi.dll
vendor/platform-tools/etc/...
```

3. Соберите приложение: `scripts\build_windows.ps1`.

## Лицензия

Platform-Tools распространяются Google по **Android SDK License Agreement**
(https://developer.android.com/studio/terms). SparkTech не скачивает их
автоматически и не распространяет. Если вы включаете adb в свою сборку, вы
самостоятельно отвечаете за соблюдение лицензии Google.

Каталог `vendor/platform-tools/` исключён из git (см. `.gitignore`), чтобы
бинарники Google не попадали в репозиторий.

@echo off
rem Установка сторожа traidingbot в автозагрузку текущего пользователя.
rem Запустите этот файл двойным кликом ОДИН раз.
copy /Y "%~dp0traidingbot_watchdog.vbs" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\traidingbot_watchdog.vbs"
if %errorlevel%==0 (
    echo Готово: сторож будет запускаться при входе в Windows.
    wscript "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\traidingbot_watchdog.vbs"
    echo Сторож запущен и сейчас.
) else (
    echo ОШИБКА копирования — запустите ещё раз или скопируйте файл вручную.
)
pause

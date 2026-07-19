@echo off
title Bot Musique Discord

echo ========================================
echo  Bot Musique Discord
echo ========================================
echo.

:: Check Java (with fallback to local install)
where java >nul 2>&1
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Java\jdk-21.0.11+10\bin\java.exe" (
        set "PATH=%LOCALAPPDATA%\Java\jdk-21.0.11+10\bin;%PATH%"
        echo [INFO] Java trouve dans %%LOCALAPPDATA%%\Java
    ) else (
        echo [ERREUR] Java 17+ requis - https://adoptium.net/
        pause
        exit /b 1
    )
)

:: Load .env variables
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
)
if "%DISCORD_TOKEN%"=="" (
    echo [ERREUR] DISCORD_TOKEN non defini. Cree un fichier .env avec :
    echo   DISCORD_TOKEN=votre_token_ici
    pause
    exit /b 1
)
if "%LAVALINK_URI%"=="" set "LAVALINK_URI=http://localhost:2333"
if "%LAVALINK_PASSWORD%"=="" set "LAVALINK_PASSWORD=youshallnotpass"

:: Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Python introuvable dans le PATH.
    pause
    exit /b 1
)

:: Install dependencies
echo [INFO] Installation des dependances...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    pause
    exit /b 1
)

:: Generate application.yml from template + .env
echo [INFO] Generation de application.yml...
python generate_yml.py

:: Check Lavalink.jar
if not exist "lavalink\Lavalink.jar" (
    echo [ERREUR] lavalink\Lavalink.jar introuvable.
    echo Telecharge-le depuis :
    echo https://github.com/lavalink-devs/Lavalink/releases
    echo.
    echo Plugins a placer dans lavalink/ :
    echo   - youtube-plugin-VERSION.jar
    echo   - lavasrc-plugin-VERSION.jar
    pause
    exit /b 1
)

:: Start Lavalink (background process)
echo [INFO] Demarrage de Lavalink...
start /B "" java -jar lavalink\Lavalink.jar
if %errorlevel% neq 0 (
    echo [ERREUR] Impossible de demarrer Lavalink.
    pause
    exit /b 1
)

echo [INFO] Attente de Lavalink (7 sec)...
timeout /t 7 /nobreak >nul

:: Start bot
echo [INFO] Demarrage du bot...
python bot.py

:: Cleanup
echo [INFO] Arret. Fermeture de Lavalink...
taskkill /f /im java.exe >nul 2>&1
pause

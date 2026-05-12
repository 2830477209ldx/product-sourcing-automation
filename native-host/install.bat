@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  Product Sourcing - Native Host Installer
echo ============================================
echo.

set "HOST_NAME=com.product_sourcing.server_launcher"
set "SCRIPT_DIR=%~dp0"
set "HOST_PATH=%SCRIPT_DIR%host.bat"

:: Get extension ID from user
echo To find your extension ID:
echo   1. Open chrome://extensions
echo   2. Enable "Developer mode"
echo   3. Find "Product Sourcing Importer"
echo   4. Copy the ID shown below the extension name
echo.
set /p EXT_ID="Enter your Chrome extension ID: "

if "%EXT_ID%"=="" (
    echo ERROR: Extension ID cannot be empty.
    pause
    exit /b 1
)

:: Create manifest JSON
set "MANIFEST_PATH=%SCRIPT_DIR%%HOST_NAME%.json"
set "HOST_PATH_ESCAPED=%HOST_PATH:\=\\%"

(
echo {
echo   "name": "%HOST_NAME%",
echo   "description": "Starts the Product Sourcing API server",
echo   "path": "%HOST_PATH_ESCAPED%",
echo   "type": "stdio",
echo   "allowed_origins": ["chrome-extension://%EXT_ID%/"]
echo }
) > "%MANIFEST_PATH%"

echo.
echo Created manifest: %MANIFEST_PATH%

:: Register in Windows Registry (current user)
set "REG_KEY=HKCU\Software\Google\Chrome\NativeMessagingHosts\%HOST_NAME%"
reg add "%REG_KEY%" /ve /t REG_SZ /d "%MANIFEST_PATH%" /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: Native messaging host registered!
    echo.
    echo Now restart Chrome and the extension will auto-start the server.
) else (
    echo.
    echo ERROR: Failed to write registry key.
)

echo.
pause

@echo off
rem ---------------------------------------------------------------------------
rem  ComfyWebStudio launcher for Windows.
rem
rem  Sets up whatever is missing on first run, then starts the server and opens
rem  a browser.
rem
rem    start.bat                start normally (builds the UI once, then serves)
rem    start.bat --dev          run the Vite dev server too, with hot reload
rem    start.bat --port 9000    serve on a different port
rem    start.bat --setup        install/refresh dependencies and exit
rem    start.bat --no-browser   don't open a browser
rem ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=8500"
set "DEV=0"
set "OPEN_BROWSER=1"
set "SETUP_ONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--dev"        ( set "DEV=1" & shift & goto parse_args )
if /I "%~1"=="--setup"      ( set "SETUP_ONLY=1" & shift & goto parse_args )
if /I "%~1"=="--no-browser" ( set "OPEN_BROWSER=0" & shift & goto parse_args )
if /I "%~1"=="--port"       ( set "PORT=%~2" & shift & shift & goto parse_args )
if /I "%~1"=="-h"           goto show_help
if /I "%~1"=="--help"       goto show_help
echo Unknown option: %~1  (try --help)
exit /b 1

:show_help
for /f "tokens=1,* delims=:" %%a in ('findstr /b /c:"rem " "%~f0"') do echo %%b
exit /b 0

:args_done

rem -- Python -----------------------------------------------------------------

set "PYTHON="
for %%P in (python py python3) do (
    if not defined PYTHON (
        where %%P >nul 2>&1 && (
            rem 3.12+ is required; older versions fail on modern typing syntax.
            %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PYTHON=%%P"
        )
    )
)

if not defined PYTHON (
    echo.
    echo   Python 3.12 or newer is required but was not found.
    echo   Install it from https://www.python.org/downloads/ and be sure to
    echo   tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\" (
    echo Creating the Python environment ^(first run only^)...
    %PYTHON% -m venv .venv || goto failed
)

set "VENV_PY=.venv\Scripts\python.exe"

if not exist ".venv\.deps-installed" (
    echo Installing Python dependencies...
    "%VENV_PY%" -m pip install --quiet --upgrade pip || goto failed
    "%VENV_PY%" -m pip install --quiet -e ".[dev]" || goto failed
    echo. > ".venv\.deps-installed"
) else (
    rem Reinstall when the dependency list changed since the last successful install.
    for /f %%i in ('powershell -NoProfile -Command "if ((Get-Item pyproject.toml).LastWriteTime -gt (Get-Item '.venv\.deps-installed').LastWriteTime) { 'yes' } else { 'no' }" 2^>nul') do set "STALE=%%i"
    if /I "!STALE!"=="yes" (
        echo Dependencies changed, reinstalling...
        "%VENV_PY%" -m pip install --quiet -e ".[dev]" || goto failed
        echo. > ".venv\.deps-installed"
    )
)

rem -- Node (frontend only) ---------------------------------------------------

set "HAVE_NODE=0"
where node >nul 2>&1 && set "HAVE_NODE=1"

if "%HAVE_NODE%"=="1" (
    if not exist "frontend\node_modules\" (
        echo Installing frontend dependencies ^(first run only^)...
        pushd frontend
        call npm install --no-fund --no-audit || ( popd & goto failed )
        popd
    )
    if "%DEV%"=="0" if not exist "frontend\dist\index.html" (
        echo Building the interface...
        pushd frontend
        call npm run build || ( popd & goto failed )
        popd
    )
) else (
    if not exist "frontend\dist\index.html" (
        echo.
        echo   Node.js was not found and the interface has not been built.
        echo   Install Node 20+ from https://nodejs.org then run: start.bat --setup
        echo   The API alone is still usable at /docs.
        echo.
    )
)

if "%SETUP_ONLY%"=="1" (
    echo Setup complete.
    exit /b 0
)

rem -- Run --------------------------------------------------------------------

if "%DEV%"=="1" (
    if "%HAVE_NODE%"=="0" (
        echo --dev needs Node.js installed.
        exit /b 1
    )
    echo Starting the Vite dev server on http://localhost:5173
    start "ComfyWebStudio UI" cmd /c "cd frontend && npm run dev"
    if "%OPEN_BROWSER%"=="1" start "" "http://localhost:5173"
    echo Starting the API on http://127.0.0.1:%PORT%
    "%VENV_PY%" -m comfywebstudio.main --port %PORT% --reload
    goto done
)

echo.
echo   ComfyWebStudio -^> http://127.0.0.1:%PORT%
echo   Press Ctrl+C to stop.
echo.
if "%OPEN_BROWSER%"=="1" start "" "http://127.0.0.1:%PORT%"
"%VENV_PY%" -m comfywebstudio.main --port %PORT%

:done
endlocal
exit /b 0

:failed
echo.
echo   Setup failed. See the messages above.
echo.
pause
exit /b 1

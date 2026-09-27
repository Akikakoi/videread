@echo off
rem ==========================================================
rem  videread one-click launcher  (dev doc: section 4 / 12)
rem  First run: create .venv, install deps, fetch ffmpeg -> bin\
rem  Usage: run.bat                       start web console + open browser (default)
rem         run.bat web                   same as above (explicit)
rem         run.bat <url> [--mode brief --no-cache --open ...]   CLI passthrough
rem ==========================================================
chcp 65001 >nul
cd /d "%~dp0"
setlocal

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\src"
set "CODE=0"
if "%~1"=="" set "HOLD=1"

echo ==========================================
echo   videread launcher
echo ==========================================
echo.

rem ---------- 1/4 Python ----------
set "BOOTSTRAP="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "BOOTSTRAP=py -3"
if defined BOOTSTRAP goto :python_ok
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "BOOTSTRAP=python"
if defined BOOTSTRAP goto :python_ok
echo [X] Python 3.11+ not found. Install it first:
echo     https://www.python.org/downloads/
goto :fail

:python_ok
echo [1/4] Python OK

rem ---------- 2/4 virtualenv + dependencies ----------
if exist "%VENV_PY%" goto :venv_ok
echo [2/4] First run: creating .venv and installing deps (needs network) ...
%BOOTSTRAP% -m venv "%ROOT%\.venv"
if errorlevel 1 goto :venv_fail
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -e "%ROOT%" --quiet
if errorlevel 1 goto :venv_fail
echo       dependencies installed
echo       (for pytest: .venv\Scripts\pip install -e ".[dev]")
goto :ffmpeg

:venv_ok
echo [2/4] .venv found, skip install
goto :ffmpeg

:venv_fail
echo [X] Failed to create .venv or install dependencies.
echo     Check your network / proxy settings.
goto :fail

rem ---------- 3/4 ffmpeg ----------
:ffmpeg
if not exist "%ROOT%\bin\ffmpeg.exe" goto :ffmpeg_path
if not exist "%ROOT%\bin\ffprobe.exe" goto :ffmpeg_path
echo [3/4] ffmpeg OK (project bin\)
goto :env

:ffmpeg_path
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :ffmpeg_sys
echo [3/4] ffmpeg not found, downloading static build into bin\ ...
call :fetch_ffmpeg
if not exist "%ROOT%\bin\ffmpeg.exe" goto :ffmpeg_warn
echo       download OK
goto :env

:ffmpeg_sys
echo [3/4] ffmpeg OK (system PATH)
goto :env

:ffmpeg_warn
echo       [!] Download failed. Put ffmpeg.exe / ffprobe.exe into bin\ manually,
echo           otherwise the "audio" stage will fail.
goto :env

rem ---------- 4/4 config ----------
:env
if exist "%ROOT%\.env" goto :env_ok
copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
echo [4/4] Created .env from .env.example
echo.
echo [!] No keys yet. Edit this file, then run this script again:
echo     %ROOT%\.env   -^>   DASHSCOPE_API_KEY / LLM_API_KEY
goto :fail

:env_ok
echo [4/4] config OK

rem ---------- run ----------
if /i "%~1"=="web" goto :run_web
if not "%~1"=="" goto :run_args
echo.
echo No arguments given, starting the local web console ...
echo (CLI passthrough is still available: run.bat "URL" --mode brief --open)
goto :run_web

:run_args
echo.
"%VENV_PY%" -m videread.cli %*
set "CODE=%ERRORLEVEL%"
goto :done

:run_web
echo.
echo Starting local web console at http://127.0.0.1:8765 ...
"%VENV_PY%" -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo       installing web dependencies ...
  "%VENV_PY%" -m pip install -e "%ROOT%" --quiet
  if errorlevel 1 goto :venv_fail
)
"%VENV_PY%" -m videread.web --open
set "CODE=%ERRORLEVEL%"
goto :done

rem ---------- wrap up ----------
:done
echo.
if not "%CODE%"=="0" goto :done_fail
echo [OK] Done.
if defined HOLD pause
exit /b 0

:done_fail
echo [X] Failed with exit code %CODE%, see the error above.
if defined HOLD pause
exit /b %CODE%

:fail
echo.
if defined HOLD pause
exit /b 1

rem ---------- subroutine: download ffmpeg / ffprobe static build ----------
:fetch_ffmpeg
if not exist "%ROOT%\bin" mkdir "%ROOT%\bin"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $base='https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0/'; $dst='%ROOT%\bin'; foreach($n in @('ffmpeg','ffprobe')){ try { $gz=Join-Path $env:TEMP ($n+'.exe.gz'); Invoke-WebRequest -Uri ($base+$n+'-win32-x64.gz') -OutFile $gz -UseBasicParsing; $exe=Join-Path $dst ($n+'.exe'); if(Test-Path $exe){Remove-Item $exe -Force}; $in=[IO.File]::OpenRead($gz); $out=[IO.File]::Create($exe); $z=New-Object IO.Compression.GZipStream($in,[IO.Compression.CompressionMode]::Decompress); $z.CopyTo($out); $z.Dispose(); $out.Dispose(); $in.Dispose(); Remove-Item $gz -Force } catch { Write-Host ('  failed: '+$n+' - '+$_.Exception.Message); exit 1 } }"
exit /b 0
@echo off
REM One-command build for the QUANTUM DEVS UI.
REM   build.bat          -> x64-Debug
REM   build.bat Release  -> x64-Release

setlocal
set "CFG=%~1"
if "%CFG%"=="" set "CFG=Debug"

set "VSDIR=C:\Program Files\Microsoft Visual Studio\18\Community"
if not exist "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" (
    set "VSDIR=C:\Program Files\Microsoft Visual Studio\2022\Community"
)

if not exist "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" (
    echo [X] vcvars64.bat not found. Open "x64 Native Tools Command Prompt"
    echo     for VS, or run this from the Visual Studio Developer Command Prompt.
    exit /b 1
)

set "CMAKEBIN=%VSDIR%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin"
set "NINJABIN=%VSDIR%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja"

call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul
set "PATH=%CMAKEBIN%;%NINJABIN%;%PATH%"

cd /d "%~dp0"

cmake -S . -B "UI\out\build" -G Ninja -DCMAKE_BUILD_TYPE=%CFG%
if errorlevel 1 exit /b 1

cmake --build "UI\out\build"
if errorlevel 1 exit /b 1

echo.
echo [+] built: UI\out\build\QuantumDevsDiscordServerCloner.exe
endlocal

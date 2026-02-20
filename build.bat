@echo off
echo ============================================================
echo  Resonance – Windows build
echo ============================================================
echo.

echo [1/2] Installing / updating PyInstaller...
pip install pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo [2/2] Building Resonance.exe...
pyinstaller resonance.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. See output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete!
echo  Executable : dist\Resonance.exe
echo.
echo  IMPORTANT: copy your .env file into dist\ before running,
echo  so Resonance.exe can find your API key:
echo.
echo    copy .env dist\
echo ============================================================
echo.
pause

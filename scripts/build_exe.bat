@echo off
REM Build keil2clangd-reanchor.exe (PyInstaller onefile).
REM Output: dist\keil2clangd-reanchor.exe  -- copy it next to your project's .clangd.
cd /d "%~dp0"
py -3 -m PyInstaller --version >nul 2>nul || py -3 -m pip install pyinstaller
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed -- check network/proxy and retry.
    exit /b 1
)
py -3 -m PyInstaller --onefile --console --name keil2clangd-reanchor ReAnchor.py
echo.
echo Done: %~dp0dist\keil2clangd-reanchor.exe

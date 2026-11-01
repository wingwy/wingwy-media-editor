@echo off
chcp 65001 >nul
echo ============================================
echo   wingwy音视频剪辑器 - 打包 exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.8+ 并加入 PATH
    pause
    exit /b 1
)

echo [1/2] 检查 PyInstaller...
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo       未安装，正在安装 PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo [2/2] 开始打包...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name wingwy-media-editor wingwy_media_editor.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上面的日志
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包完成：dist\wingwy-media-editor.exe
echo   注意：exe 不包含 FFmpeg，首次运行请点右上角「获取 FFmpeg」
echo ============================================
pause

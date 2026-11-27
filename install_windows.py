"""Install the Windows preview for the current Windows user."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
PROGRAM_FILES = (
    'Start-GHG.bat', 'local_server.py', 'index.html', 'xlsx_local.py',
    'inventory_quality.py', 'annual_report.py', 'inventory_years.py', 'local_auth.py', 'login.html', 'verification_export.py',
)


def create_shortcut(launcher):
    env = dict(os.environ, GHG_LAUNCHER=str(launcher))
    command = """
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop 'GHG System V2.lnk'))
$link.TargetPath = $env:GHG_LAUNCHER
$link.WorkingDirectory = Split-Path $env:GHG_LAUNCHER
$link.Description = 'GHG System V2.0 Preview'
$link.Save()
"""
    windows = Path(os.environ['SystemRoot'])
    powershell = windows / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                   env=env, check=True)


def install():
    if sys.platform != 'win32':
        raise OSError('安裝程式只適用於 Windows。')
    if sys.version_info < (3, 10):
        raise OSError('請使用 Python 3.10 或以上版本。')
    root = Path(os.environ['LOCALAPPDATA']) / 'GHGSystemApp'
    root.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix='version-', dir=root))
    try:
        for name in PROGRAM_FILES:
            shutil.copy2(SOURCE / name, destination / name)
        create_shortcut(destination / 'Start-GHG.bat')
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(destination, ignore_errors=True)
        raise


if __name__ == '__main__':
    try:
        install()
    except (OSError, KeyError, subprocess.SubprocessError) as error:
        print(f'安裝未完成：{error}')
        sys.exit(1)
    print('安裝完成。請從桌面開啟 GHG System V2。')

# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Resonance.
Build with:  pyinstaller resonance.spec --clean
Output:      dist\Resonance.exe
"""

block_cipher = None

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('whisper_flow/sounds/*.mp3', 'sounds'),
    ],
    hiddenimports=[
        'keyboard._winkeyboard',
        'sounddevice',
        'numpy',
        'dotenv',
        'pyperclip',
        'pyautogui',
        'pyscreeze',
        'PIL',
        'PIL.Image',
        'miniaudio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Collect all sounddevice data/binaries (includes PortAudio DLL)
from PyInstaller.utils.hooks import collect_all
sd_datas, sd_binaries, sd_hiddenimports = collect_all('sounddevice')
a.datas    += sd_datas
a.binaries += sd_binaries
a.hiddenimports += sd_hiddenimports

np_datas, np_binaries, np_hiddenimports = collect_all('numpy')
a.datas    += np_datas
a.binaries += np_binaries
a.hiddenimports += np_hiddenimports

ma_datas, ma_binaries, ma_hiddenimports = collect_all('miniaudio')
a.datas    += ma_datas
a.binaries += ma_binaries
a.hiddenimports += ma_hiddenimports

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Resonance',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can break sounddevice DLLs; keep off
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

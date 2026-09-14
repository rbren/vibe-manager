#!/usr/bin/env python3
"""Stage a standalone Canvas App using an explicit source-only allowlist."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'extensions/kanban-manager'


def package(destination: Path):
    if destination.exists():
        raise ValueError('Use a new staging directory; never overwrite a checkout')
    destination.mkdir(parents=True)
    for name in ('canvas-extension.json', 'extension.js', 'package.json', 'package-lock.json',
                 'build.mjs', 'validate.mjs', 'README.md'):
        shutil.copy2(APP / name, destination / name)
    for directory, suffixes in [('src', {'.js', '.jsx', '.css'}), ('test', {'.js', '.mjs', '.py'})]:
        for source in (APP / directory).rglob('*'):
            if source.is_file() and source.suffix in suffixes and '__pycache__' not in source.parts:
                target = destination / source.relative_to(APP)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    shutil.copy2(ROOT / 'static/style.css', destination / 'src/board.css')
    backend = destination / 'backend'
    backend.mkdir()
    for name in ('app.py', 'requirements.txt'):
        shutil.copy2(ROOT / name, backend / name)
    for directory, suffixes in [('sidecar', {'.py'}), ('automation', {'.py'}), ('static', {'.js', '.css', '.html'})]:
        for source in (ROOT / directory).rglob('*'):
            if source.is_file() and source.suffix in suffixes and '__pycache__' not in source.parts:
                target = backend / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    (destination / '.gitignore').write_text('node_modules/\ndist/\n__pycache__/\n*.pyc\n')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    package(parser.parse_args().destination.resolve())

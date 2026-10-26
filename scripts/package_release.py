"""Package an existing Windows build without copying its runtime/user data."""
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=root / 'dist/releases/0.1.1/BBBCompanion')
    parser.add_argument('--output', type=Path, default=root / 'dist/github/bbb-faker-0.1.1-windows.zip')
    args = parser.parse_args()
    app_dir, output = args.app_dir.resolve(), args.output.resolve()
    if output.is_relative_to(app_dir):
        raise ValueError('The archive must be outside the application directory')
    allowed = ['BBBCompanion.exe', '_internal', 'browsers']
    for name in allowed:
        if not (app_dir / name).exists():
            raise FileNotFoundError(app_dir / name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in allowed:
            source = app_dir / name
            files = [source] if source.is_file() else sorted(source.rglob('*'))
            for path in files:
                if path.is_file():
                    if not path.resolve().is_relative_to(app_dir):
                        raise ValueError(f'External symlink in build: {path}')
                    archive.write(path, Path('BBBCompanion') / path.relative_to(app_dir))
        # Always take public documentation and example from the source tree,
        # even when the existing build still has an older, personalized example.
        for name in ['README.md', 'config.example.json']:
            archive.writestr('BBBCompanion/' + name, (root / name).read_bytes())
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert not any('/data/' in name or '/diagnostics/' in name for name in names)
        assert any('/browsers/chromium-' in name and name.endswith('chrome.exe') for name in names)
        assert archive.read('BBBCompanion/config.example.json') == (root / 'config.example.json').read_bytes()
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    checksum = output.with_suffix(output.suffix + '.sha256')
    checksum.write_text(f'{digest}  {output.name}\n', encoding='utf-8')
    print(output)
    print(f'{output.stat().st_size:,} bytes; SHA-256 {digest}')
    print(checksum)


if __name__ == '__main__':
    main()

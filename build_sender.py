"""Build a sender-only wheel and documentation bundle; never include runtime archives."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', help='Reuse a wheel after verifying all sender source bytes')
    args = parser.parse_args()
    output = ROOT / 'dist-mis' / 'sender'
    output.mkdir(parents=True, exist_ok=True)
    if args.wheel:
        wheel = Path(args.wheel).resolve()
    else:
        stage = ROOT/'build-mis'/uuid.uuid4().hex
        (stage/'sender').mkdir(parents=True)
        for path in (ROOT/'sender').glob('*.py'):
            shutil.copyfile(str(path), str(stage/'sender'/path.name))
        for name in ('pyproject.toml', 'README.md'):
            shutil.copyfile(str(ROOT/name), str(stage/name))
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(stage), '--no-deps',
                        '--no-build-isolation', '--wheel-dir', str(output)], check=True)
        candidates = list(output.glob('mpnano_sender-*.whl'))
        if len(candidates) != 1:
            raise RuntimeError('Multiple versions present; explicitly select --wheel')
        wheel = candidates[0]
    with zipfile.ZipFile(str(wheel)) as archive:
        names = archive.namelist()
        if any(not (n.startswith('sender/') or n.startswith('mpnano_sender-') and '.dist-info/' in n) for n in names):
            raise RuntimeError('Unexpected component in wheel')
        sources = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in (ROOT/'sender').glob('*.py')}
        if {n for n in names if n.startswith('sender/')} != set(sources):
            raise RuntimeError('Wheel source file set mismatch')
        if any(archive.read(n) != data for n, data in sources.items()):
            raise RuntimeError('Wheel differs from current sender sources')
    files = {wheel.name: wheel, 'README.md': ROOT/'README.md', 'requirements.txt': ROOT/'requirements.txt'}
    for folder, pattern in (('docs', '*.md'), ('examples', '*.json')):
        for path in (ROOT/folder).glob(pattern):
            files[path.relative_to(ROOT).as_posix()] = path
    manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    bundle = output/('mpnano-sender-' + wheel.name.split('-')[1] + '-candidate.zip')
    with zipfile.ZipFile(str(bundle), 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.write(str(path), name)
        archive.writestr('SHA256.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    (output/'SHA256.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'bundle': str(bundle), 'wheel': str(wheel), 'files': len(files),
                      'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest()}))


if __name__ == '__main__': main()

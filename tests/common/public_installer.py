"""Local release downloads for exercising the exact generated Bash installer."""


def fake_release_curl(path, assets):
    """Emulate GitHub asset delivery without network access or accepting arbitrary URLs."""
    path.write_text('''#!/usr/bin/env python3
import pathlib, sys, urllib.parse
arguments = sys.argv[1:]
url = next(a for a in arguments if a.startswith('https://'))
name = urllib.parse.urlsplit(url).path.rsplit('/', 1)[1]
source = pathlib.Path(ASSET_DIRECTORY) / name
if not source.is_file():
    sys.exit(22)
body = source.read_bytes()
if '--output' in arguments:
    pathlib.Path(arguments[arguments.index('--output') + 1]).write_bytes(body)
else:
    sys.stdout.buffer.write(body)
'''.replace('ASSET_DIRECTORY', repr(str(assets))))
    path.chmod(0o755)

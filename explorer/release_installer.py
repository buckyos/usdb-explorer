"""Render one self-contained installer pinned to an exact public-service release archive."""
import shlex

from install_release import RELEASE_ID, sha256


def render_installer(source, archive, release_id, *, base_url=None):
    """Embed trusted installer code and the archive hash; runtime arguments cannot replace either."""
    if RELEASE_ID.fullmatch(release_id) is None or archive.name != release_id + ".tar.gz":
        raise ValueError("invalid release-bound installer input")
    repository = "usdb-explorer" if release_id.startswith("usdb-explorer-") else "usdb"
    tag = release_id.removeprefix("usdb-explorer-") if repository == "usdb-explorer" else release_id
    url = base_url or f"https://github.com/buckyos/{repository}/releases/download/{tag}"
    if not url.startswith("https://") or any(c.isspace() for c in url) or "'" in url:
        raise ValueError("release asset URL must be HTTPS")
    code = (source / "install_release.py").read_text()
    if "USDB_PUBLIC_INSTALLER_PY" in code:
        raise ValueError("installer source conflicts with the shell heredoc delimiter")
    return f'''#!/usr/bin/env bash
set -euo pipefail
umask 077

release_id={shlex.quote(release_id)}
archive_sha256={shlex.quote(sha256(archive))}
archive_url={shlex.quote(url.rstrip('/') + '/' + archive.name)}

if [[ "${{1:-}}" == "--help" || "${{1:-}}" == "-h" ]]; then
  cat <<'USDB_PUBLIC_INSTALLER_HELP'
Install this pinned USDB Explorer release for the current user.
  --install-root DIR  Version storage (default ~/.local/share/usdb-public)
  --bin-dir DIR       Command directory (default ~/.local/bin)
  --config-file FILE  Preserve or create configuration (default ~/.config/usdb-public/config.json)
Existing configuration, deployment data and running services are preserved.
USDB_PUBLIC_INSTALLER_HELP
  exit 0
fi

for command in python3 curl sha256sum mktemp; do
  command -v "$command" >/dev/null 2>&1 || {{
    echo "Required command is not installed: $command" >&2
    exit 1
  }}
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
temporary="$(mktemp -d "${{TMPDIR:-/tmp}}/.usdb-public-installer.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
archive="$temporary/$release_id.tar.gz"
echo "Downloading $release_id"
curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \\
  --tlsv1.2 --retry 3 --connect-timeout 30 --max-time 300 --max-filesize 16777216 \\
  "$archive_url" --output "$archive"
printf '%s  %s\\n' "$archive_sha256" "$archive" | sha256sum -c - >/dev/null

# Frozen arguments follow user options, so they cannot be replaced by runtime flags.
python3 - "$@" --archive "$archive" --release-id "$release_id" --expected-sha256 "$archive_sha256" <<'USDB_PUBLIC_INSTALLER_PY'
{code}
USDB_PUBLIC_INSTALLER_PY
'''

"""``SSH_ASKPASS`` helper.

OpenSSH runs this program when it needs a password or key passphrase and reads
the secret from stdout. The secret is handed over in a one-shot file whose path
arrives in ``REMOTELY_ASKPASS_FILE``; the file is unlinked as soon as it has
been read, so a replay of the same command yields nothing.

The file lives in ``~/.remotely/run`` (mode 0700) and is itself mode 0600. This
is the same exposure as a private key on disk, but measured in milliseconds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_FILE = "REMOTELY_ASKPASS_FILE"


def main() -> int:
    path_raw = os.environ.get(ENV_FILE)
    if not path_raw:
        # No secret was staged. Returning non-zero makes ssh fall back to its
        # own prompt rather than treating an empty string as the password.
        print("remotely-askpass: no secret staged", file=sys.stderr)
        return 1

    path = Path(path_raw)
    try:
        secret = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"remotely-askpass: cannot read secret: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # ssh strips exactly one trailing newline; write the secret verbatim.
    sys.stdout.write(secret)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

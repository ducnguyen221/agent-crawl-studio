"""A self-signed certificate made at run time with the OpenSSL of Git for Windows (or any `openssl` on PATH).

Nothing is stored in the repository: the files go to a directory the test gives (pytest `tmp_path`).
No OpenSSL found -> TlsUnavailable, which the tests turn into a BLOCKED_ENV skip (never a pass)."""

import os
import shutil
import subprocess

CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
)


class TlsUnavailable(Exception):
    pass


def find_openssl():
    found = shutil.which("openssl")
    if found:
        return found
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    raise TlsUnavailable("no openssl found (PATH and the usual Git for Windows folders)")


def make_cert(directory, name="pinned.example"):
    """Return (cert_path, key_path) of a self-signed certificate valid one day for DNS:`name`."""
    openssl = find_openssl()
    directory = os.fspath(directory)
    cfg = os.path.join(directory, "openssl.cnf")
    crt = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    with open(cfg, "w", encoding="ascii") as handle:
        handle.write("[req]\ndistinguished_name=d\nx509_extensions=v\nprompt=no\n[d]\nCN=%s\n"
                     "[v]\nsubjectAltName=DNS:%s\nbasicConstraints=CA:TRUE\n" % (name, name))
    env = dict(os.environ, MSYS_NO_PATHCONV="1", OPENSSL_CONF=cfg)
    done = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                           "-days", "1", "-config", cfg], capture_output=True, env=env, timeout=60)
    if done.returncode != 0 or not os.path.exists(crt):
        raise TlsUnavailable("openssl failed with exit code %d" % done.returncode)
    return crt, key

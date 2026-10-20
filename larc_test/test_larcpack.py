#!/usr/bin/env python3
"""
test_larcpack.py -- round trip test for larcpack.py.

For every method LArc supports the test

  1. packs a few files with larcpack.py
  2. unpacks them again with larcpack.py and compares the bytes
  3. (if msdos.exe and LARC.EXE are available) lets the *real* LARC.EXE list
     and extract the archive and compares the bytes as well

usage:  python test_larcpack.py [--no-larc]
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import larcpack
import larc_format as L

MSDOS = os.path.join(REPO, "msdos.exe")
LARC_EXE = os.path.join(REPO, "LARC.EXE")
LARC_OVR = os.path.join(REPO, "LARC7E.OVR")

PAYLOADS = {
    "hello.txt": (b"Hello LArc world!\r\n"
                  b"The quick brown fox jumps over the lazy dog.\r\n"
                  b"0123456789 " * 8),
    "repeat.bin": b"ABCD" * 1024,
    "pseudo.bin": bytes(((i * 7) ^ (i >> 3)) & 0xFF for i in range(1500)),
}

METHODS = [("4", []), ("5", []), ("8", []),
           ("1", ["--pos-bits", "11", "--len-bits", "4"]),
           ("2", ["--pos-bits", "12", "--len-bits", "4"]),
           ("3", ["--pos-bits", "11", "--len-bits", "5"])]


def make_payloads(d):
    for name, data in PAYLOADS.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)


def check_with_larc(archive, work, expect):
    """let the real LARC.EXE extract the archive and compare"""
    if not (os.path.exists(MSDOS) and os.path.exists(LARC_EXE)):
        return "skipped (no msdos.exe/LARC.EXE)"
    d = os.path.join(work, "larc_out")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for f in (LARC_EXE, LARC_OVR):
        shutil.copy(f, d)
    shutil.copy(archive, d)
    r = run([MSDOS, "LARC.EXE", "E", "/o", os.path.basename(archive)], d)
    out = r.stdout.decode("latin1")
    if "CRC Error" in out:
        return "FAIL (LArc reported a CRC error)"
    bad = []
    for name, data in expect.items():
        p = os.path.join(d, name)
        if not os.path.exists(p):
            bad.append("%s missing" % name)
            continue
        with open(p, "rb") as f:
            if f.read() != data:
                bad.append("%s differs" % name)
    if bad:
        return "FAIL (" + ", ".join(bad) + ")"
    return "ok"


def main():
    use_larc = "--no-larc" not in sys.argv
    work = tempfile.mkdtemp(prefix="larcpack_test_")
    failures = 0
    try:
        src = os.path.join(work, "src")
        os.makedirs(src)
        make_payloads(src)

        for method, extra in METHODS:
            d = os.path.join(work, "m" + method)
            os.makedirs(d)
            for f in os.listdir(src):
                shutil.copy(os.path.join(src, f), d)
            arc = os.path.join(d, "test.lzs")

            r = run([sys.executable, os.path.join(HERE, "larcpack.py"), "A",
                     "-lz%s-" % method] + extra + ["test.lzs"] +
                    sorted(PAYLOADS), d)
            if r.returncode != 0:
                print("FAIL method %s: packing failed\n%s"
                      % (method, r.stdout.decode("latin1")))
                failures += 1
                continue

            # the packer must be able to read it back
            out = os.path.join(d, "out")
            os.makedirs(out)
            r = run([sys.executable, os.path.join(HERE, "larcpack.py"), "E",
                     "-o", os.path.join("..", "test.lzs")], out)
            ok = True
            for name, data in PAYLOADS.items():
                p = os.path.join(out, name)
                if not os.path.exists(p):
                    print("FAIL method %s: %s not extracted" % (method, name))
                    ok = False
                    failures += 1
                    continue
                with open(p, "rb") as f:
                    if f.read() != data:
                        print("FAIL method %s: %s differs" % (method, name))
                        ok = False
                        failures += 1

            # and the real LArc must be able to read it
            state = "skipped"
            if use_larc and ok:
                state = check_with_larc(arc, work, PAYLOADS)
                if state.startswith("FAIL"):
                    failures += 1
            print("method -lz%s-  packer: %-4s  LARC.EXE: %s"
                  % (method, "ok" if ok else "FAIL", state))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("")
    print("all ok" if failures == 0 else "%d failure(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

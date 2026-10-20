#!/usr/bin/env python
"""
make_test_archives.py -- build LArc test archives for every method that
LARC.EXE understands.

  -lz4-   stored
  -lz5-   LZSS, 4 KiB window (this is what LArc itself writes when the data
          compresses; it falls back to -lz4- otherwise)
  -lzs-   LZSS, 2 KiB window, bit oriented
  -lz7-   accepted by LArc 7, but there is no codec for it: LArc stores the
          member as it is and *skips* the payload when extracting

Run:  python make_test_archives.py [outdir]
"""

import json
import os
import sys

import larc_format as L

HERE = os.path.dirname(os.path.abspath(__file__))

TEXT = (b"Hello LArc world!\r\n"
        b"The quick brown fox jumps over the lazy dog.\r\n"
        b"0123456789 " * 8)

REPEAT = b"ABCD" * 1024                       # 4096 bytes, very compressible

PSEUDO = bytes(((i * 7) ^ (i >> 3)) & 0xFF for i in range(1500))


def payloads():
    return {
        "hello.txt": TEXT,
        "repeat.bin": REPEAT,
        "pseudo.bin": PSEUDO,
    }


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "archives")
    paydir = os.path.join(outdir, "payload")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(paydir, exist_ok=True)
    pl = payloads()

    for name, data in pl.items():
        with open(os.path.join(paydir, name), "wb") as f:
            f.write(data)

    made = []

    def add(arcname, members, note):
        blob = L.build_archive(members)
        path = os.path.join(outdir, arcname)
        with open(path, "wb") as f:
            f.write(blob)
        info = {"archive": arcname, "note": note, "size": len(blob),
                "members": []}
        pos = 0
        while pos < len(blob) and blob[pos] != 0:
            m, pos = L.parse_member(blob, pos)
            info["members"].append({
                "name": m["name"], "method": m["method"],
                "original_size": m["original_size"],
                "compressed_size": m["compressed_size"],
                "crc16": m["crc"], "data_offset": m["data_offset"],
            })
        made.append(info)
        print("%-16s %6d bytes  %s" % (arcname, len(blob),
                                       " ".join("%s:%s" % (m["method"], m["name"])
                                                for m in info["members"])))

    # ---- one archive per method -------------------------------------------
    add("t_lz4.lzs",
        [("hello.txt", pl["hello.txt"], "-lz4-"),
         ("repeat.bin", pl["repeat.bin"], "-lz4-")],
        "stored (uncompressed) members")

    add("t_lz5.lzs",
        [("hello.txt", pl["hello.txt"], "-lz5-"),
         ("repeat.bin", pl["repeat.bin"], "-lz5-"),
         ("pseudo.bin", pl["pseudo.bin"], "-lz5-")],
        "LZSS 4 KiB window, byte oriented")

    # ---- the LZSS bit oriented family (methods 1..3) ----------------------
    # the two bit widths live in the extended part of the member header
    def lzs_member(name, payload, method, posbits, lenbits):
        return (name, payload, method,
                L.encode_lzs(payload, posbits, lenbits),
                bytes([posbits, lenbits]))

    add("t_lzs.lzs",
        [lzs_member("hello.txt", pl["hello.txt"], "-lzs-", 11, 4),
         lzs_member("repeat.bin", pl["repeat.bin"], "-lzs-", 11, 4),
         lzs_member("pseudo.bin", pl["pseudo.bin"], "-lzs-", 11, 4)],
        "LZSS, bit oriented, 2 KiB window (11 bit index + 4 bit length). "
        "The bit widths are taken from the extended member header")

    add("t_lz123.lzs",
        [lzs_member("hello.txt", pl["hello.txt"], "-lz1-", 11, 4),
         lzs_member("repeat.bin", pl["repeat.bin"], "-lz2-", 12, 4),
         lzs_member("pseudo.bin", pl["pseudo.bin"], "-lz3-", 11, 5)],
        "methods 1, 2 and 3: one and the same bit oriented LZSS decoder, "
        "only the level parameters in the header differ")

    add("t_lz8.lzs",
        [("hello.txt", pl["hello.txt"], "-lz8-",
          L.encode_lz5(pl["hello.txt"])),
         ("repeat.bin", pl["repeat.bin"], "-lz8-",
          L.encode_lz5(pl["repeat.bin"]))],
        "method 8 carries a -lz5- stream: method 8 uses the same byte "
        "oriented LZSS codec as method 5")

    add("t_lz7.lzs",
        [("hello.txt", pl["hello.txt"], "-lz7-")],
        "method 7 has no codec: LArc takes the VMA/book path for it and does "
        "not even list such an archive")

    # ---- a mixed archive ---------------------------------------------------
    add("t_mixed.lzs",
        [("hello.txt", pl["hello.txt"], "-lz5-"),
         ("repeat.bin", pl["repeat.bin"], "-lz4-"),
         lzs_member("pseudo.bin", pl["pseudo.bin"], "-lzs-", 11, 4)],
        "all supported methods in one archive")

    # ---- reference: exactly what LARC.EXE produces ------------------------
    # LArc writes -lz5- when the compressed form is smaller, -lz4- otherwise.
    members = []
    members.append(("hello.txt", pl["hello.txt"],
                    "-lz5-" if len(L.encode_lz5(pl["hello.txt"])) < len(pl["hello.txt"])
                    else "-lz4-"))
    members.append(("repeat.bin", pl["repeat.bin"], "-lz5-"))
    add("t_native.lzs", members,
        "same policy as LArc itself (compressed if it is smaller)")

    # ---- negative tests ----------------------------------------------------
    blob = bytearray(L.build_archive([("hello.txt", pl["hello.txt"], "-lz5-")]))
    blob[1] ^= 0xFF                       # break the header checksum
    with open(os.path.join(outdir, "t_badhdr.lzs"), "wb") as f:
        f.write(blob)
    made.append({"archive": "t_badhdr.lzs",
                 "note": "header checksum deliberately wrong", "size": len(blob),
                 "members": []})

    blob = bytearray(L.build_archive([("hello.txt", pl["hello.txt"], "-lz5-")]))
    hdr_end = 2 + blob[0]                      # the CRC sits at the end of the header
    blob[hdr_end - 2] ^= 0xFF                  # break the stored CRC-16
    blob[1] = sum(blob[2:hdr_end]) & 0xFF     # keep the header checksum valid
    with open(os.path.join(outdir, "t_badcrc.lzs"), "wb") as f:
        f.write(blob)
    made.append({"archive": "t_badcrc.lzs",
                 "note": "member CRC-16 deliberately wrong", "size": len(blob),
                 "members": []})

    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump({"payloads": {k: len(v) for k, v in pl.items()},
                   "archives": made}, f, indent=2)
    print("\nmanifest.json written to", outdir)


if __name__ == "__main__":
    main()

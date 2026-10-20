#!/usr/bin/env python3
"""
larcpack.py -- a LArc (.lzs) archiver with selectable compression methods
and compression parameters.

The command line follows LArc / LHarc:

    larcpack.py COMMAND [/[-]SWITCH] archive[.lzs] [file...]

commands (LArc compatible)
    A   add files to the archive (creating it when needed)
    F   freshen: add only the files that are newer than the archived copy
    E   extract files (all when no name is given)
    L   list the archive
    D   delete members from the archive
    P   print the member data to stdout

switches (LArc compatible, '/' or '-' prefix)
    -lzN-   the compression method.  N is the LArc method number:
               1,2,3  bit oriented LZSS, parameters taken from the
                      --pos-bits/--len-bits options (default 11/4 = 2 KiB)
               4      stored
               5,8    byte oriented LZSS, 4 KiB window
               7      the "opaque" method: the payload is kept as it is,
                      LArc does not decode it (it is the marker LArc uses
                      for the members of a VMA/"book")
    -o      overwrite existing files without asking
    -m      suppress the messages
    -n      name only (listing)
    -x      extract with path (rebuild the directory tree)
    -e PATH extract into PATH
    -d      confirm the delete
    -l N    screen lines
    -c      do not check the time stamps
    -r      reflexive

extensions
    --pos-bits N   position bit count of the bit oriented LZSS (methods 1..3).
                   The window is 1 << N.  LArc's own dictionary is 8 KiB, so
                   N must not exceed 13.
    --len-bits M   length bit count of the bit oriented LZSS (methods 1..3).
                   The maximum match length is 2**M + (N+M+1)//9.
    --no-mangle    store the file names as given instead of the DOS 8.3 form
    --verify      check the CRC of every member while listing

The archive format is the one LArc 3.33 / "LArc 7" uses:

    +0x00 BYTE   header size (bytes from +0x02 to the end of the header)
    +0x01 BYTE   header checksum = sum(bytes[+0x02..end]) & 0xFF
    +0x02 5      method id, e.g. "-lz5-"
    +0x07 DWORD  compressed size
    +0x0B DWORD  original size
    +0x0F DWORD  MS-DOS time stamp
    +0x13 BYTE   MS-DOS attribute (0x10 = directory)
    +0x14 BYTE   level
    +0x15 BYTE   file name length
    +0x16 ...    file name
    +0x16+n ...  LArc 7 extended area (only for the methods 1..3):
                 byte 0 = position bit count, byte 1 = length bit count
    end-2  WORD  CRC-16 of the original file
           ...   member data

The archive ends with a 0x00 byte.  See ../LARC.lst for the details.
"""

import os
import sys
import glob
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import larc_format as L

# --------------------------------------------------------------------------
# method table
# --------------------------------------------------------------------------

STORED = 4
LZS = (1, 2, 3)          # bit oriented LZSS, parameterised
LZ5 = (5, 8)             # byte oriented LZSS, 4 KiB window
OPAQUE = 7                # no codec at all

DEFAULT_POS_BITS = 11
DEFAULT_LEN_BITS = 4


def method_id(number):
    """the 5 character method id LArc puts into the header"""
    return "-lz%d-" % number


def method_needs_ext(number):
    """the methods 1..3 carry their level parameters in the extended header"""
    return number in LZS


def compress(number, data, pos_bits=DEFAULT_POS_BITS, len_bits=DEFAULT_LEN_BITS,
            ext=b""):
    """returns (body, ext) for the given method"""
    if number in LZ5:
        return L.encode_lz5(data), b""
    if number in LZS:
        if len(ext) >= 2:
            pos_bits, len_bits = ext[0], ext[1]
        return (L.encode_lzs(data, pos_bits, len_bits),
                bytes([pos_bits, len_bits]))
    # 4 (stored) and 7 (opaque) keep the data as it is
    return data, b""


# --------------------------------------------------------------------------
# MS-DOS time stamp
# --------------------------------------------------------------------------

def dos_stamp(t=None):
    if t is None:
        t = time.localtime()
    if t.tm_year < 1980:
        t = time.localtime(315532800)          # 1980-01-01
    date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return (tm & 0xFFFF) | ((date & 0xFFFF) << 16)


def from_dos_stamp(stamp):
    tm = stamp & 0xFFFF
    date = (stamp >> 16) & 0xFFFF
    return time.mktime(((date >> 9) + 1980, (date >> 5) & 0x0F, date & 0x1F,
                         (tm >> 11) & 0x1F, (tm >> 5) & 0x3F, (tm & 0x1F) * 2,
                         0, 0, -1))


# --------------------------------------------------------------------------
# DOS 8.3 names
# --------------------------------------------------------------------------

def mangle_name(name):
    """turn a name into the DOS 8.3 form LArc stores (LONGER.TXT -> LONGER~1.TXT)"""
    path, _, base = name.replace("/", "\\").rpartition("\\")
    stem, _, ext = base.partition(".")
    stem = stem.upper()
    ext = ext.upper()
    if not stem:
        return name
    if len(stem) <= 8 and len(ext) <= 3:
        return name                       # already 8.3
    short = stem[:6] + "~" if len(stem) > 8 else stem[:8]
    ext = ext[:3]
    n = 1
    while True:
        cand = (short + str(n)) if short.endswith("~") else short
        if ext:
            cand += "." + ext
        probe = os.path.join(path, cand) if path else cand
        if not os.path.exists(probe):
            break
        n += 1
    return (path + "\\" + cand) if path else cand


# --------------------------------------------------------------------------
# archive i/o
# --------------------------------------------------------------------------

class Member(object):
    def __init__(self, name, data, method, attr=0x20, stamp=None, ext=b"",
                 level=0, osize=None):
        self.name = name
        self.data = data                # the *original* data
        self.method = method
        self.attr = attr
        self.stamp = stamp if stamp is not None else dos_stamp()
        self.ext = ext
        self.level = level
        self.osize = osize if osize is not None else len(data)

    @property
    def crc(self):
        return L.crc16(self.data)


def method_number(tag):
    """the LArc method id ("-lz5-") -> the method number (5)"""
    try:
        return int(tag.strip("-")[2:])
    except ValueError:
        return 0


def read_archive(path):
    """returns the list of members of an archive (the data is decoded)"""
    with open(path, "rb") as f:
        blob = f.read()
    out = []
    pos = 0
    while pos < len(blob) and blob[pos] != 0:
        info, nxt = L.parse_member(blob, pos)
        raw = blob[info["data_offset"]:info["data_offset"] + info["compressed_size"]]
        body = decode_body(info["method"], raw, info)
        out.append(Member(info["name"], body, method_number(info["method"]),
                          info["attribute"], info["stamp"], ext=info["ext"],
                          osize=info["original_size"]))
        pos = nxt
    return out


def write_archive(path, members):
    out = bytearray()
    for m in members:
        body, ext = compress(m.method, m.data, ext=m.ext)
        out += L.build_member(m.name, m.data, method_id(m.method), attr=m.attr,
                              stamp=m.stamp, level=m.level, raw=body, ext=ext)
    out += b"\x00"
    with open(path, "wb") as f:
        f.write(bytes(out))


def decode_body(method_id_str, raw, info=None):
    """decode a member payload (only the methods LArc can decode)"""
    if method_id_str in ("-lz5-", "-lz8-"):
        return L.decode_lz5(raw)
    if method_id_str in ("-lzs-", "-lz1-", "-lz2-", "-lz3-"):
        pos_bits, len_bits = DEFAULT_POS_BITS, DEFAULT_LEN_BITS
        if info and len(info.get("ext") or b"") >= 2:
            pos_bits, len_bits = info["ext"][0], info["ext"][1]
        return L.decode_lzs(raw, None, pos_bits, len_bits)
    return raw                       # stored / opaque


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

USAGE = __doc__.split("The archive format")[0]


class Options(object):
    def __init__(self):
        self.command = None
        self.method = None            # the method number
        self.pos_bits = DEFAULT_POS_BITS
        self.len_bits = DEFAULT_LEN_BITS
        self.archive = None
        self.files = []
        self.overwrite = False
        self.quiet = False
        self.name_only = False
        self.with_path = False
        self.extract_path = None
        self.confirm_delete = False
        self.screen_lines = 0
        self.no_time_check = False
        self.reflexive = False
        self.mangle = True
        self.verify = False


def parse_args(argv):
    o = Options()
    args = list(argv)
    if not args:
        raise SystemExit(USAGE)
    cmd = args.pop(0)
    if cmd.upper() not in ("A", "F", "E", "L", "D", "P", "S"):
        raise SystemExit("unknown command %r\n%s" % (cmd, USAGE))
    o.command = cmd.upper()

    while args:
        a = args.pop(0)
        if a.startswith("--"):
            name = a[2:].lower()
            if name == "pos-bits":
                o.pos_bits = int(args.pop(0))
            elif name == "len-bits":
                o.len_bits = int(args.pop(0))
            elif name == "no-mangle":
                o.mangle = False
            elif name == "verify":
                o.verify = True
            else:
                raise SystemExit("unknown option %r" % a)
            continue
        if a.startswith("-") or a.startswith("/"):
            sw = a[1:]
            low = sw.lower()
            if low.startswith("lz") and len(sw) >= 3 and sw[2].isdigit():
                # -lzN-   or   -lzN-P-M-  (the latter is an extension)
                o.method = int(sw[2])
                if "-" in sw[3:]:
                    parts = sw[3:].split("-")
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        o.pos_bits = int(parts[0])
                        o.len_bits = int(parts[1])
                continue
            if len(sw) == 1:
                c = low
                if c == "o":
                    o.overwrite = True
                elif c == "m":
                    o.quiet = True
                elif c == "n":
                    o.name_only = True
                elif c == "x":
                    o.with_path = True
                elif c == "d":
                    o.confirm_delete = True
                elif c == "c":
                    o.no_time_check = True
                elif c == "r":
                    o.reflexive = True
                elif c == "l":
                    o.screen_lines = int(args.pop(0))
                elif c == "e":
                    o.extract_path = args.pop(0)
                else:
                    raise SystemExit("bad switch %r" % a)
                continue
            raise SystemExit("bad switch %r" % a)
        # the first plain word is the archive, the rest are the file names
        if o.archive is None:
            o.archive = a
        else:
            o.files.append(a)
    if o.archive is None:
        raise SystemExit(USAGE)
    if not o.archive.lower().endswith(".lzs"):
        o.archive += ".lzs"
    return o


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def say(opt, text):
    if not opt.quiet:
        sys.stdout.write(text)
        sys.stdout.flush()


def expand(patterns):
    """the file arguments -> a list of (path, name) pairs"""
    out = []
    for pat in patterns:
        if os.path.isdir(pat):
            for p in sorted(os.listdir(pat)):
                full = os.path.join(pat, p)
                if os.path.isfile(full):
                    out.append((full, os.path.join(pat, p)))
            continue
        hits = sorted(glob.glob(pat))
        if not hits:
            out.append((pat, pat))       # let the caller report the error
            continue
        for h in hits:
            if os.path.isfile(h):
                out.append((h, h))
    return out


def store_name(opt, name):
    """the name as LArc stores it: the path as typed, upper case, 8.3"""
    n = name.replace("/", "\\")
    if opt.mangle:
        n = mangle_name(n)
    return n.upper()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_add(opt, freshen=False):
    members = []
    if os.path.exists(opt.archive):
        members = read_archive(opt.archive)
    known = {m.name: m for m in members}

    added = 0
    for path, name in expand(opt.files):
        if not os.path.exists(path):
            sys.stderr.write("%s: file not found\n" % name)
            continue
        arcname = store_name(opt, name)
        if freshen and arcname in known:
            try:
                # the MS-DOS time stamp has a resolution of 2 seconds
                if os.path.getmtime(path) <= from_dos_stamp(known[arcname].stamp) + 2:
                    continue
            except OSError:
                pass
        with open(path, "rb") as f:
            data = f.read()
        st = os.stat(path)
        attr = 0x10 if os.path.isdir(path) else (st.st_file_attributes
                                                if hasattr(st, "st_file_attributes")
                                                else 0x20)
        if os.path.isdir(path):
            data = b""
        method = opt.method
        ext = b""
        if method is None:
            # the policy of LArc itself: compress with -lz5- and keep the
            # data as it is when that does not make it smaller
            body = L.encode_lz5(data)
            if len(body) < len(data):
                method = 5
            else:
                method = 4
        elif method in LZS:
            ext = bytes([opt.pos_bits, opt.len_bits])
        m = Member(arcname, data, method, attr=attr,
                   stamp=dos_stamp(time.localtime(st.st_mtime)), ext=ext)
        if freshen and arcname in known:
            members = [x for x in members if x.name != arcname]
        members.append(m)
        added += 1
        say(opt, "%s\r\n" % arcname)

    if not added and not os.path.exists(opt.archive):
        sys.stderr.write("%s: no files added\n" % opt.archive)
        return 1
    write_archive(opt.archive, members)
    say(opt, "Done.   (100%)\r\n")
    return 0


def cmd_list(opt):
    if not os.path.exists(opt.archive):
        sys.stderr.write("%s: dose not exist\n" % opt.archive)
        return 1
    members = read_archive(opt.archive)
    if opt.name_only:
        for m in members:
            print(m.name)
        return 0
    print("Name         Date      Time     Length   Size Now "
          "Ratio     Attr   CRC  Type Sub")
    print("=" * 13 + " " + "=" * 8 + "  " + "=" * 8 + " " + "=" * 8 + " "
          + "=" * 8 + " " + "=" * 8 + " " + "=" * 8)
    total_o = total_c = 0
    for m in members:
        st = time.localtime(from_dos_stamp(m.stamp))
        body, _ = compress(m.method, m.data, ext=m.ext)
        csize = len(body)
        isdir = (m.attr & 0x10) != 0
        crc = "%04X" % m.crc
        if opt.verify and not isdir:
            if L.crc16(decode_body(method_id(m.method), body)) != m.crc:
                crc = "BAD"
        if isdir:
            size = "<DIR>"
            ratio = "100.00%"
        else:
            size = "%d" % csize
            ratio = "%.2f%%" % (100.0 if not m.osize
                                else min(100.0, csize * 100.0 / m.osize))
        now = ""
        if os.path.exists(m.name):
            now = "%d" % os.path.getsize(m.name)
        print("%-13s" % m.name)
        print("             %-8s  %-8s %8d %8s %4s %9s %-7s %-5s %-4s %-3s"
              % (time.strftime("%y/%m/%d", st), time.strftime("%H:%M:%S", st),
                 m.osize, size, now, ratio, attr_str(m.attr), crc, "T", m.method))
        total_o += m.osize
        total_c += csize
    print("        " + "=" * 8 + " " + "=" * 8 + " " + "=" * 8 + " "
          + "=" * 8 + " " + "=" * 8 + " " + "=" * 8)
    print("%-13s %-8d %-8s %8d %8d %4s %9s"
          % ("Total", len(members), "", total_o, total_c, "",
             "%.2f%%" % (100.0 if not total_o
                         else min(100.0, total_c * 100.0 / total_o))))
    return 0


def attr_str(attr):
    """the DOS attribute flags, the way LArc prints them (rhsvda)"""
    out = ""
    for bit, ch in ((0x01, "r"), (0x02, "h"), (0x04, "s"), (0x08, "v"),
                    (0x10, "d"), (0x20, "a")):
        out += ch if attr & bit else "-"
    return out


def cmd_extract(opt):
    if not os.path.exists(opt.archive):
        sys.stderr.write("%s: dose not exist\n" % opt.archive)
        return 1
    members = read_archive(opt.archive)
    wanted = [f.upper() for f in opt.files]
    if wanted:
        members = [m for m in members
                   if m.name.upper() in wanted
                   or os.path.basename(m.name).upper() in wanted]
    if opt.extract_path:
        if not os.path.isdir(opt.extract_path):
            os.makedirs(opt.extract_path)
        os.chdir(opt.extract_path)

    for m in members:
        isdir = (m.attr & 0x10) != 0
        name = m.name
        if not opt.with_path:
            name = os.path.basename(name.replace("\\", os.sep))
        if isdir:
            if not os.path.isdir(name):
                if not opt.overwrite:
                    sys.stdout.write("%sCreate Directory ? [Y/N] " % name)
                    sys.stdout.flush()
                    ans = input_answer()
                    if not ans:
                        continue
                try:
                    os.makedirs(name, exist_ok=True)
                except OSError as e:
                    sys.stderr.write("%s: can not create directory\n" % name)
                    continue
            say(opt, "UnPacking....\r\n")
            continue
        d = os.path.dirname(name)
        if d and not os.path.isdir(d):
            if not opt.overwrite:
                sys.stdout.write("%sCreate Directory ? [Y/N] " % d)
                sys.stdout.flush()
                if not input_answer():
                    continue
            os.makedirs(d, exist_ok=True)
        if os.path.exists(name) and not opt.overwrite:
            sys.stdout.write("%sOver write ? [Y/N] " % name)
            sys.stdout.flush()
            if not input_answer():
                continue
        say(opt, "UnPacking....\r\n")
        with open(name, "wb") as f:
            f.write(m.data)
        try:
            os.utime(name, (from_dos_stamp(m.stamp),) * 2)
        except OSError:
            pass
    say(opt, "Done.\r\n")
    return 0


def input_answer():
    try:
        a = sys.stdin.readline()
    except Exception:
        return False
    return a.strip()[:1].upper() == "Y"


def cmd_delete(opt):
    if not os.path.exists(opt.archive):
        sys.stderr.write("%s: dose not exist\n" % opt.archive)
        return 1
    members = read_archive(opt.archive)
    wanted = [f.upper() for f in opt.files]
    if not wanted:
        sys.stderr.write("no member given\n")
        return 1
    if opt.confirm_delete:
        sys.stdout.write("delete ? [Y/N] ")
        sys.stdout.flush()
        if not input_answer():
            return 0
    keep = [m for m in members
            if m.name.upper() not in wanted
            and os.path.basename(m.name).upper() not in wanted]
    write_archive(opt.archive, keep)
    say(opt, "Done.\r\n")
    return 0


def cmd_print(opt):
    if not os.path.exists(opt.archive):
        sys.stderr.write("%s: dose not exist\n" % opt.archive)
        return 1
    for m in read_archive(opt.archive):
        if opt.files and m.name.upper() not in [f.upper() for f in opt.files]:
            continue
        sys.stdout.buffer.write(m.data)
    return 0


def main(argv):
    opt = parse_args(argv)
    if opt.method is not None and opt.method not in (1, 2, 3, 4, 5, 7, 8):
        sys.stderr.write("method %d is not supported by LArc "
                         "(1,2,3,4,5,7,8)\n" % opt.method)
        return 2
    if opt.method in LZS:
        if opt.pos_bits > 13:
            sys.stderr.write("pos-bits %d does not fit the 8 KiB LArc "
                             "dictionary (max 13)\n" % opt.pos_bits)
            return 2
        if (1 << opt.len_bits) + (opt.pos_bits + opt.len_bits + 1) // 9 \
                > (1 << opt.pos_bits):
            sys.stderr.write("len-bits %d does not fit the window of %d "
                             "position bits\n" % (opt.len_bits, opt.pos_bits))
            return 2
    if opt.command == "A":
        return cmd_add(opt, freshen=False)
    if opt.command == "F":
        return cmd_add(opt, freshen=True)
    if opt.command == "E":
        return cmd_extract(opt)
    if opt.command == "L":
        return cmd_list(opt)
    if opt.command == "D":
        return cmd_delete(opt)
    if opt.command == "P":
        return cmd_print(opt)
    if opt.command == "S":
        sys.stderr.write("the self extract format of LArc is a special .COM "
                         "stub and is not implemented here\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
larc_format.py -- LArc (LArc Ver3.33 / "LArc 7") archive writer.

Everything in here was derived from LARC.EXE + LARC7E.OVR (see ../LARC.lst):

  * container layout (LHA-ish header, "generalized LHA format"):
        +0x00  WORD  header size  (bytes from +0x02 up to the end of the header)
        +0x01  BYTE  header checksum = sum(bytes[+0x02 .. end]) & 0xFF
        +0x02  5     method id string, e.g. "-lz5-"
        +0x07  DWORD compressed size
        +0x0B  DWORD original size
        +0x0F  DWORD MS-DOS timestamp (WORD time, WORD date)
        +0x13  BYTE  MS-DOS attribute
        +0x14  BYTE  level
        +0x15  BYTE  file name length
        +0x16  ...   file name (MS-DOS, upper case)
        +0x16+n WORD  CRC-16 of the *original* file (little endian)
        then the member data, and the archive ends with a 0x00 byte.

  * "-lz4-"  stored, no compression
  * "-lz5-"  LZSS, 4 KiB window, byte oriented
  * "-lzs-"  LZSS, 2 KiB window, bit oriented (11 bit index, 4 bit length)
  * "-lz7-"  accepted by LArc 7 but *not* implemented: the member payload is
             skipped over during extraction (LARC7E.OVR ovr004:0x083A).

The two LZSS codecs were reverse engineered from the decoders in LARC7E.OVR:

  -lz5- decoder  : ovr004:0x0CDF  sub_2E05F   (seed: ovr004:0x0CAE sub_2E02E)
  -lzs- decoder  : ovr004:0x0B28  sub_2DEA8
"""

import struct
import time

# --------------------------------------------------------------------------
# CRC-16 (the one LArc uses: reflected, poly 0xA001, init 0)
# --------------------------------------------------------------------------

def crc16(data, crc=0):
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc & 0xFFFF


# --------------------------------------------------------------------------
# the LArc image: the seed of the "-lz5-" dictionary
#   13 bytes of every byte value, an ascending ramp, a descending ramp,
#   128 NULs and 110 spaces  ->  4078 bytes; the write cursor starts at 4078
# --------------------------------------------------------------------------

def larc_image():
    img = bytearray()
    for v in range(256):
        img += bytes([v]) * 13
    img += bytes(range(256))
    img += bytes(range(255, -1, -1))
    img += bytes(128)
    img += b" " * 110
    assert len(img) == 4078, len(img)
    return img


# --------------------------------------------------------------------------
# LZSS encoders
# --------------------------------------------------------------------------

def _lzss_encode(data, window, cursor, min_match, max_match, seed):
    """generic LZSS encoder shared by -lz5- (byte oriented) and -lzs- (bit oriented).

    Returns a list of tokens: ("lit", byte) or ("match", pos, len).
    `pos` is the absolute position inside the window (ring).

    Only bytes that the encoder itself has produced are used as match
    sources - the pre-seeded part of the dictionary is never searched
    (that is what LArc's own encoder does, see the -lz5- reference output).
    """
    hist = bytearray(window)
    hist[:len(seed)] = seed
    valid = bytearray(window)      # 1 = written by the encoder
    di = cursor
    tokens = []
    n = len(data)
    i = 0
    while i < n:
        best_len = 0
        best_pos = 0
        limit = min(max_match, n - i)
        if limit >= min_match:
            # scan the ring, most recent first
            for dist in range(1, window):
                p = (di - dist) & (window - 1)
                if not valid[p] or hist[p] != data[i]:
                    continue
                # The match may overlap the bytes that this very match is about
                # to write; those bytes are the ones we are producing now.
                l = 0
                while l < limit:
                    if l < dist:
                        if hist[(p + l) & (window - 1)] != data[i + l]:
                            break
                    else:
                        if data[i + l - dist] != data[i + l]:
                            break
                    l += 1
                if l > best_len:
                    best_len = l
                    best_pos = p
                    if l == limit:
                        break
        if best_len >= min_match:
            tokens.append(("match", best_pos, best_len))
            for k in range(best_len):
                # The bytes that this match produces are the bytes of
                # the input data, also for the part of the match that
                # overlaps the bytes that are being written.
                hist[di] = data[i + k]
                valid[di] = 1
                di = (di + 1) & (window - 1)
            i += best_len
        else:
            tokens.append(("lit", data[i]))
            hist[di] = data[i]
            valid[di] = 1
            di = (di + 1) & (window - 1)
            i += 1
    return tokens


def encode_lz5(data):
    """-lz5- : LZSS with a 4 KiB window, byte oriented.

    * the dictionary is the LArc image, the write cursor starts at 4078
    * a control byte precedes every 8 tokens, bit i (LSB first) belongs to
      token i, a set bit means "literal", a clear bit means "match"
    * literal : 1 byte
    * match  : b0 = pos & 0xFF, b1 = ((pos >> 8) & 0x0F) | ((len - 3) << 4)
               len is 3..18
    """
    tokens = _lzss_encode(data, 4096, 4078, 3, 18, larc_image())
    out = bytearray()
    for base in range(0, len(tokens), 8):
        group = tokens[base:base + 8]
        flags = 0
        body = bytearray()
        for idx, t in enumerate(group):
            if t[0] == "lit":
                flags |= 1 << idx
                body.append(t[1])
            else:
                _, pos, ln = t
                body.append(pos & 0xFF)
                body.append((((pos >> 8) & 0x0F) << 4) | ((ln - 3) & 0x0F))
        out.append(flags)
        out += body
    return bytes(out)


class _BitWriter:
    """MSB first bit writer (the LArc getbit routine reads the MSB first)."""

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value, count):
        for i in range(count - 1, -1, -1):
            bit = (value >> i) & 1
            self.acc = (self.acc << 1) | bit
            self.nbits += 1
            if self.nbits == 8:
                self.out.append(self.acc)
                self.acc = 0
                self.nbits = 0

    def flush(self):
        if self.nbits:
            self.out.append(self.acc << (8 - self.nbits))
            self.acc = 0
            self.nbits = 0
        return bytes(self.out)


def encode_lzs(data, posbits=11, lenbits=4):
    """-lzs- : LZSS, bit oriented.

    * the dictionary is filled with spaces
    * 1 bit: 1 -> literal (8 bits), 0 -> match
    * match: `posbits` bit absolute index, `lenbits` bit length
    * the window is 1 << posbits, the write cursor starts at
      window - (1 << lenbits) - (posbits + lenbits + 1) // 9
    * the decoded length is  (posbits+lenbits+1)//9 + field + 1

    The two bit widths are *not* fixed: LArc 7 takes them from the extended
    part of the member header (see build_member's `ext`).
    """
    ce2 = (posbits + lenbits + 1) // 9
    window = 1 << posbits
    cursor = window - (1 << lenbits) - ce2
    if cursor < 0:
        raise ValueError("invalid level: %d/%d would give a negative write "
                         "cursor (2**%d + %d must be <= 2**%d)"
                         % (posbits, lenbits, lenbits, ce2, posbits))
    if posbits > 13:
        raise ValueError("posbits=%d does not fit the 8 KiB LArc dictionary "
                         "(the decoder would overwrite its own variables)" % posbits)
    maxlen = (1 << lenbits) - 1 + ce2 + 1
    tokens = _lzss_encode(data, window, cursor, 3, min(maxlen, 17), b" " * window)
    w = _BitWriter()
    for t in tokens:
        if t[0] == "lit":
            w.write(1, 1)
            w.write(t[1], 8)
        else:
            _, pos, ln = t
            w.write(0, 1)
            w.write(pos, posbits)
            w.write(ln - ce2 - 1, lenbits)
    return w.flush()


def decode_lz5(data, outlen=None):
    """-lz5- decoder: byte oriented LZSS, 4 KiB window.

    One control byte per 8 tokens, bit i (LSB first) belongs to token i,
    a set bit = literal, a clear bit = match.  A match is two bytes:
    b0 = pos & 0xFF, b1 = ((pos >> 8) & 0x0F) << 4 | (len - 3).
    """
    hist = bytearray(4096)
    hist[:len(larc_image())] = larc_image()
    mask = 4095
    di = 4078
    out = bytearray()
    i = 0
    while i < len(data):
        flags = data[i]
        i += 1
        for bit in range(8):
            if i >= len(data) and (outlen is None or len(out) >= (outlen or 0)):
                break
            if flags & (1 << bit):
                if i >= len(data):
                    break
                c = data[i]
                i += 1
                hist[di] = c
                di = (di + 1) & mask
                out.append(c)
            else:
                if i + 1 >= len(data):
                    break
                b0, b1 = data[i], data[i + 1]
                i += 2
                pos = b0 | ((b1 >> 4) << 8)
                ln = (b1 & 0x0F) + 3
                for _ in range(ln):
                    c = hist[pos & mask]
                    hist[di] = c
                    pos = (pos + 1) & mask
                    di = (di + 1) & mask
                    out.append(c)
            if outlen is not None and len(out) >= outlen:
                break
        if outlen is not None and len(out) >= outlen:
            break
    return bytes(out)


def decode_lzs(data, outlen=None, posbits=11, lenbits=4):
    """-lzs- decoder: bit oriented LZSS.

    1 bit: 1 -> literal (8 bits), 0 -> match (posbits position, lenbits
    length).  The decoded length is (posbits+lenbits+1)//9 + field + 1.
    """
    ce2 = (posbits + lenbits + 1) // 9
    window = 1 << posbits
    mask = window - 1
    hist = bytearray(window)
    for k in range(window):
        hist[k] = 0x20
    di = window - (1 << lenbits) - ce2
    out = bytearray()
    bitpos = 0
    nbits = len(data) * 8

    def get(n):
        nonlocal bitpos
        v = 0
        for _ in range(n):
            if bitpos >= nbits:
                raise EOFError
            v = (v << 1) | ((data[bitpos >> 3] >> (7 - (bitpos & 7))) & 1)
            bitpos += 1
        return v

    try:
        while outlen is None or len(out) < outlen:
            if get(1):
                c = get(8)
                hist[di] = c
                di = (di + 1) & mask
                out.append(c)
            else:
                pos = get(posbits)
                ln = get(lenbits) + ce2 + 1
                for _ in range(ln):
                    c = hist[pos & mask]
                    hist[di] = c
                    pos = (pos + 1) & mask
                    di = (di + 1) & mask
                    out.append(c)
    except EOFError:
        pass
    return bytes(out)


ENCODERS = {
    "-lz5-": encode_lz5,
    "-lzs-": encode_lzs,
    "-lz4-": None,          # stored
}


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

def dos_time_stamp(t=None):
    if t is None:
        t = time.localtime()
    dos_date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return dos_time | (dos_date << 16)


def build_member(name, payload, method="-lz5-", attr=0x20, stamp=None,
                 level=0, raw=None, ext=b""):
    """build one archive member (header + data).

    `ext` are the LArc 7 "extended header" bytes that sit between the file
    name and the CRC-16: byte 0 = the position bit count of the LZSS
    decoder, byte 1 = the length bit count.  They are only used by the
    methods 1..3 (-lz1- .. -lz3- / -lzs-); the CRC-16 always stays the
    last two bytes of the header.
    """
    if raw is None:
        enc = ENCODERS.get(method)
        body = payload if enc is None else enc(payload)
    else:
        body = raw
    name = name.upper()
    head = bytearray()
    head.append(0)                 # header size, patched below
    head.append(0)                 # checksum, patched below
    head += method.encode("ascii")
    head += struct.pack("<II", len(body), len(payload))
    head += struct.pack("<I", stamp if stamp is not None else dos_time_stamp())
    head.append(attr)
    head.append(level)
    head.append(len(name))
    head += name.encode("ascii")
    head += ext                     # <- LArc 7 extended header
    head += struct.pack("<H", crc16(payload))
    head[0] = len(head) - 2
    head[1] = sum(head[2:]) & 0xFF
    return bytes(head) + body


def build_archive(members, terminator=b"\x00"):
    """members: list of (name, payload, method) or (name, payload, method, raw)
    or ready made member blobs."""
    out = bytearray()
    for m in members:
        if isinstance(m, bytes):
            out += m
        elif len(m) == 4:
            name, payload, method, raw = m
            out += build_member(name, payload, method, raw=raw)
        elif len(m) == 5:
            name, payload, method, raw, ext = m
            out += build_member(name, payload, method, raw=raw, ext=ext)
        else:
            name, payload, method = m
            out += build_member(name, payload, method)
    out += terminator
    return bytes(out)


def parse_member(blob, pos=0):
    """parse one member; returns (dict, next_pos)."""
    d = blob
    hdr_size = d[pos]
    method = d[pos + 2:pos + 7].decode("latin1")
    csize, osize = struct.unpack_from("<II", d, pos + 7)
    stamp = struct.unpack_from("<I", d, pos + 15)[0]
    attr, level, nlen = d[pos + 19], d[pos + 20], d[pos + 21]
    name = d[pos + 22:pos + 22 + nlen].decode("latin1")
    hdr_end = pos + 2 + hdr_size
    crc = struct.unpack_from("<H", d, hdr_end - 2)[0]
    ext = d[pos + 22 + nlen:hdr_end - 2]
    info = {
        "offset": pos,
        "header_size": hdr_size,
        "checksum": d[pos + 1],
        "checksum_calc": sum(d[pos + 2:hdr_end]) & 0xFF,
        "method": method,
        "compressed_size": csize,
        "original_size": osize,
        "stamp": stamp,
        "attribute": attr,
        "level": level,
        "name": name,
        "crc": crc,
        "crc_calc": crc16(d[pos + hdr_end:pos + hdr_end + osize]) if method == "-lz4-" else None,
        "ext": ext,
        "data_offset": hdr_end,
    }
    return info, hdr_end + csize

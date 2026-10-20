# LArc test archives (LARC.EXE / LARC7E.OVR)

Everything in this directory was derived from `../LARC.EXE` + `../LARC7E.OVR`
(see `../LARC.lst` for the IDA listing) and validated by running the real
`LARC.EXE` under `msdos.exe`.

```
larc_format.py            archive writer + the two LZSS codecs
make_test_archives.py     builds archives/*.lzs
verify_with_larc.ps1      runs the real LARC.EXE against them and checks the result
archives/                 the generated test archives (+ manifest.json)
archives/payload/         the original (uncompressed) payloads
```

## Container format

Generalized LHA style member header, exactly as LArc writes it:

```
+0x00  BYTE   header size = number of bytes from +0x02 to the end of the header
+0x01  BYTE   header checksum = sum(bytes[+0x02 .. end]) & 0xFF
+0x02  5      method id string, e.g. "-lz5-"
+0x07  DWORD  compressed size
+0x0B  DWORD  original size
+0x0F  DWORD  MS-DOS timestamp (WORD time, WORD date)
+0x13  BYTE   MS-DOS attribute
+0x14  BYTE   level
+0x15  BYTE   file name length
+0x16  ...    file name (MS-DOS, upper case)
+0x16+n 2      the "level field" (see below)
end-2  WORD   CRC-16 of the original file (little endian)
       ...    member data
```

The archive ends with a `0x00` byte. The CRC is **CRC-16/ARC**
(reflected, poly 0xA001, init 0) over the original data.

### The level field

The two bytes **right after the file name** are the LArc 7 "level" field.
Their meaning depends on the method:

| method | level field | effect |
|--------|-------------|--------|
| 4, 5   | the CRC-16 of the original data | LArc verifies it and reports "CRC Error" |
| 1, 2, 3 | the position bit count and the length bit count of the bit oriented LZSS | the decoder parameters |
| 8      | the same as for 1..3 | (the method 8 uses the -lz5- codec) |

LArc's own listing prints the level field in the "CRC" column for the
methods 4/5 and in two extra columns for the methods 1/2/3/8, e.g.

```
HELLO.TXT
             26/10/20  11:16:32      608      153 ( 25.16%) -----a      T2  12 4
```

(`T` = type, `2` = the method, `12 4` = the level field).

For the methods 1..3 LArc does **not** store a CRC at all, so a damaged
member is not detected.  `larcpack.py` keeps a CRC behind the level field
for those methods as well - LArc ignores it, but it makes the archive
self-checking.

### The LArc 7 extended header

The two bytes **right after the file name** are the level parameters of the
bit oriented LZSS decoder (methods 1..3):

```
+0x16+n  BYTE  position bit count  (the window is 1 << this)
+0x17+n  BYTE  length bit count
```

The CRC-16 always stays the **last two bytes of the header**, so the
extended area sits between the name and the CRC.  Everything between the
name and the CRC is free space; only the first two bytes are used.

Proven by probing the real program:

| header | result |
|--------|--------|
| no extended bytes (the CRC follows the name) | the level bytes alias the CRC -> garbage |
| 1 extra byte | one of the two level bytes still aliases the CRC -> garbage |
| 2 extra bytes `0B 04` (11 position / 4 length bits) | decodes correctly |
| 3 or 8 extra bytes | still correct (the rest is ignored) |
| extended bytes *after* the CRC | garbage - the CRC must be the last two bytes of the header |
| the bytes at a fixed offset instead of after the name | garbage - the offset is name relative |

The decoder itself (`ovr004:0x0B28` `sub_2DEA8`) derives everything from
those two bytes: the window mask is `(1 << position_bits) - 1`, the write
cursor starts at `window - (1 << length_bits) - (position_bits+length_bits+1)/9`
and the match length is `(position_bits+length_bits+1)/9 + field + 1`.

Note that LArc 3.33 / LArc 7 only ever *write* `-lz4-` and `-lz5-`, and
those two methods ignore the level bytes - which is why a plain LHA style
header (no extended area) works for them and only breaks the `-lzs-`
family.

## Method numbers

The switch parser (`LARC7E.OVR` ovr006:0x187C `sub_2FEBC`) accepts
`-lzs-` ( -> 1) and `-lz` followed by one or two digits ( -> that number,
e.g. `-lz7-` -> 7).  The number is stored in the member record at +0x5B and
validated against the set **{1,2,3,4,5,7,8}** (ovr006:0x1AFF, `byte_2FF69`);
everything else is refused.

The number is *not* one codec per number - the unpack dispatch
(ovr004:0x0725..0x0863, `sub_2DA05`) maps them like this:

| number | tag      | codec |
|--------|----------|-------|
| 1,2,3  | -lz1-, -lz2-, -lz3- | bit oriented LZSS (`sub_2DEA8`): 1 bit flag, literal = 8 bit, match = position + length.  The number only selects the *level* parameters stored in the member header (position bits / length bits, default 11/4 = 2 KiB window).  All three share the same decoder. |
| 4      | -lz4-    | stored, no compression (`sub_2D8F8`) |
| 5      | -lz5-    | byte oriented LZSS, 4 KiB window (`sub_2E05F`) |
| 7      | -lz7-    | **no codec at all** - LArc takes the VMA/book code path for it and skips the payload when extracting |
| 8      | -lz8-    | the same byte oriented LZSS as method 5 |

Verified with the real program: an archive tagged `-lz8-` whose payload is a
`-lz5-` stream unpacks perfectly, and `-lz1-`, `-lz2-`, `-lz3-` all produce
identical output for identical input (one shared decoder).

`LARC.EXE` itself only ever *writes* `-lz4-` (when compression does not pay)
and `-lz5-` (when it does); `larc_format.encode_lz5()` reproduces LArc's own
`-lz5-` output **byte for byte**.

## The two LZSS codecs

`-lz5-` (`ovr004:0x0CDF` `sub_2E05F`, seed: `ovr004:0x0CAE` `sub_2E02E`)

* dictionary: 4096 bytes, pre-seeded with the "LArc image"
  (13 bytes of every byte value, an ascending ramp, a descending ramp,
  128 NULs, 110 spaces = 4078 bytes), write cursor starts at 4078
* one control byte per 8 tokens, bit i (LSB first) belongs to token i,
  a **set** bit = literal, a **clear** bit = match
* literal: 1 byte
* match : b0 = pos & 0xFF, b1 = ((pos >> 8) & 0x0F) << 4 | (len - 3), len 3..18

`-lzs-` (`ovr004:0x0B28` `sub_2DEA8`)

* bit oriented: 1 bit -> literal (8 bit), 0 bit -> match
* match: 11 bit absolute index + 4 bit length (length = value + 2)
* dictionary filled with spaces, cursor at 2048 - 17

## Known gap

None any more: with the level field in place the `-lzs-` / `-lz1-` / `-lz2-` /
`-lz3-` family round trips byte exactly as well.

## larcpack.py - the packer

```
larcpack.py COMMAND [/[-]SWITCH] archive[.lzs] [file...]
```

commands: `A` add, `F` freshen, `E` extract, `L` list, `D` delete,
`P` print to stdout.

switches (LArc compatible, `/` or `-` prefix):

```
-lzN-     the compression method
-o         overwrite without asking      -m    suppress the messages
-n         name only (listing)           -x    extract with path
-e PATH    extract into PATH             -d    confirm the delete
-l N       screen lines                  -c    do not check the time stamps
-r         reflexive
```

extensions:

```
--pos-bits N   position bit count of the bit oriented LZSS (methods 1..3),
               the window is 1 << N (LArc's dictionary is 8 KiB, so N <= 13)
--len-bits M   length bit count of the bit oriented LZSS (methods 1..3)
--no-mangle    keep the file names as given instead of the DOS 8.3 form
--verify       check the CRC of every member while listing
```

Without a `-lzN-` switch the packer uses LArc's own policy: compress with
`-lz5-` and keep the data as it is when that does not make it smaller.

Examples:

```
larcpack.py A -lz5-  arch.lzs *.txt          # LZSS, 4 KiB window
larcpack.py A -lz1- --pos-bits 12 --len-bits 4 arch.lzs big.bin
larcpack.py L arch.lzs
larcpack.py E -o -x arch.lzs                # rebuild the directory tree
```

`test_larcpack.py` round trips every method and - if `msdos.exe` and
`LARC.EXE` are in the repository root - lets the *real* LARC.EXE extract
the result and compares the bytes.

The `S` (self extract) command of LArc is **not** implemented: its SFX is
a special 541 byte .COM stub (not an EXE with the archive appended), so it
cannot be produced by simply concatenating files.

## Running the verifier

```
cd larc_test
python make_test_archives.py
powershell -NoProfile -ExecutionPolicy Bypass -File .\verify_with_larc.ps1
```

`msdos.exe` (MS-DOS Player) has to be in the repository root; the verifier
runs `LARC.EXE L <arc>` and `LARC.EXE E /o <arc>` in a throw away directory
(`/o` = overwrite, otherwise LArc asks "Over write ?" and waits for a key).
DOS 8.3 file names are mandatory - a longer name makes LArc drop into its
interactive mode and wait forever.

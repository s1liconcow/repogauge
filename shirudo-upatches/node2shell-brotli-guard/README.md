# node2shell-brotli-guard

The `node2shell-brotli-guard` packages install a upatch that kills a
Node.js process with `SIGKILL` when the Brotli decoder it bundles is
about to switch the block type of a tree that has a single block type,
the input of Node2Shell (also called HTTP2Shell), CVE-2026-111497. Any
application that decompresses attacker-supplied Brotli data with
`node:zlib` (request bodies, `fetch()` responses, archives) is exposed
on Node.js 22 before 22.23.4, 24 before 24.22.1 and 26 before 26.12.1.

## What the bug is and what the guard refuses

The decoder reported a block switch on a tree with one block type as a
temporary need for more input instead of a format error, so the
streaming decoder kept appending input bytes to its eight-byte internal
buffer, past its end. The fix (google/brotli 0ed4268, "Detect and report
invalid stream earlier") makes `DecodeBlockTypeAndLength()` return
`BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH` when
`s->num_block_types[tree] <= 1`. No valid stream reaches that state: a
tree of one block type has nothing to switch to.

`DecodeBlockTypeAndLength()` is always inlined. Its six callers, the
literal, command and distance block switch functions in a fast and a
"safe" variant, are `BROTLI_NOINLINE`, and the official Node.js builds
name them in their `.symtab`. The upatch has one uprobe at the entry of
each. A program reads `s->num_block_types[tree]` from the decoder state
its function was given and kills the process when it is 0 or 1, before
the function body runs: exactly the input the fix refuses, and nothing
a valid stream sends.

What user space sees: the Node.js process exits on signal 9. A process
manager (systemd, a container runtime, PM2) starts it again. The
application loses its in-flight requests, which a fixed Node.js would
have answered with a `Z_DATA_ERROR` on the one bad stream. That is a
denial of service an attacker can repeat, in place of memory corruption.

## Packages and binaries

| Beefile | Package | For |
|---|---|---|
| `Beefile` | `node2shell-brotli-guard` | official Node.js 22 and 26 builds, checked on v22.23.3 and v26.12.0 |
| `v24.bee` | `node2shell-brotli-guard-v24` | official Node.js 24 builds, checked on v24.22.0 |

Both name `/usr/local/bin/node`, where the official images and a release
tarball unpacked into `/usr/local` put it. In v24.22.0, GCC cloned the
safe command and distance functions as
`SafeDecodeCommandBlockSwitch.constprop.0` and
`SafeDecodeDistanceBlockSwitch.constprop.0`, and those clones are what
the decoder calls, so v24 has its own source. A build for another path
or another release takes a copy of the source with another
`NODE2SHELL_PATH`, or other function names: `readelf -sW <node> | grep
BlockSwitch` lists them.

The builder records the binary's GNU build ID, so a upatch built against
one Node.js build loads only where that same file is installed
(`binary_mismatch` elsewhere). Build one package per Node.js build you
run, and select the nodes with labels.

Distribution packages of Node.js usually link the system
`libbrotlidec` instead of the bundled copy; a stripped library has none
of these local functions, and the builder then fails. Update that
library from the distribution.

## The offsets

`num_block_types` is an array of three 64-bit `brotli_reg_t`, at offset
`0x138` of `BrotliDecoderState` in all three builds above. The offset
moves between Brotli releases and compilers, so the upatch does not
carry it in its code: `gen_offsets.py`, the `.rodata` generator of the
Beefile, reads it at `bpm build` from the binary the source names, from
the load each guarded function makes off its first argument at its
start, and writes `brotli_num_block_types`. The literal, command and
distance functions must agree on one base `X`, at `X`, `X + 8` and
`X + 16`; anything else fails the build. Read back what a build holds:

```sh
shirudo bpm info data node2shell-brotli-guard-1.0.0.bee \
	upatches/node2shell-brotli-guard.upatch
```

A package built without the generator (`shirudo build` alone) holds 0
there, and its programs count every call in `unchecked` and refuse
nothing.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`, on a
host where the Node.js build sits at `/usr/local/bin/node`:

```sh
shirudo bpm build upatches/node2shell-brotli-guard --version 1.0.0 --keys-dir <keys>
shirudo bpm build upatches/node2shell-brotli-guard --file upatches/node2shell-brotli-guard/v24.bee \
	--version 1.0.0 --keys-dir <keys>
shirudo bpm push node2shell-brotli-guard-1.0.0.bee oci://<registry>/<namespace>/node2shell-brotli-guard --keys-dir <keys>
shirudo bpm tree append node2shell-brotli-guard-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

## Maps

| Map | Counts |
|---|---|
| `hits` | processes killed before a block switch on a tree of one block type |
| `activity` | every block switch |
| `unchecked` | block switches not checked: no offsets, or a state that could not be read |

Each is a per-CPU array of one counter; sum the CPUs.

## Preemption model and cost

The programs keep no state but per-CPU counters, which they increment
with an atomic add, as a run can be preempted by another on its CPU
under preemptible RCU. They are right on every preemption model.

A block switch happens once per block of the compressed data, not per
byte: encoders emit blocks of hundreds of symbols or more. Each switch
costs a uprobe hit (about a microsecond) and one 8-byte read. A crafted
stream can switch on every symbol and so slow its own decoding by that
much per symbol.

## Limits

- The guard turns the memory corruption into a crash of the process;
  an attacker who can send Brotli data can still crash it.
- It needs the six functions in the binary's symbol table: official
  builds have them, a stripped build does not, and the builder then
  fails with "uprobe function … not found".
- Validated here: the sources compile with shirudo's compiler flags,
  all six functions resolve to one address each in the official
  v22.23.3, v24.22.0 and v26.12.0 linux-x64 binaries, and
  `gen_offsets.py` reads `0x138`/`0x140`/`0x148` from each of them,
  which their disassembly confirms (`mov 0x138(%rdi),%r8; cmp $0x1,%r8`
  at the start of `DecodeLiteralBlockSwitch`). Not yet done: a load in
  shirudo's test VM.
- Upgrade Node.js.

# ruby2shell-casemap-guard

The `ruby2shell-casemap-guard-*` packages install a upatch that kills a
Ruby process with `SIGKILL` before a case mapping (`upcase`,
`downcase(:fold)`, `capitalize`, `swapcase` and their `!` forms) writes
past its buffer. That is Ruby2Shell, CVE-2026-132825
(GHSA-34m4-m9fh-gqp5), in Ruby before 3.3.14, 3.4.13 and 4.0.10, and in
every Ruby 3.2, which is past its end of life and gets no fix. The
guard also refuses the case-mapping buffers beyond `INT_MAX` of
GHSA-87fm-7w84-j2px, fixed in the same releases, for these encodings.

## What the bug is and what the guard refuses

The `case_map()` functions of fourteen single-byte encodings
(ISO-8859-1, -2, -3, -4, -9, -10, -13, -14, -15, -16 and Windows-1250,
-1252, -1254, -1257) map the sharp s, `0xDF`, to two bytes under upcase
or fold, but ran their loop while one byte of room was left in the
output buffer, so the second byte could land one past it. The fix
(ruby/ruby d582f8f) stops the loop with one byte of room left, and
`rb_str_casemap()` continues in a new buffer. GHSA-87fm (bdff3c5d66dd)
keeps each buffer within `INT_MAX`.

The upatch has one sleepable uprobe at the entry of each encoding's
`case_map()`, in its `enc/<encoding>.so`. A program reads the flags and
the source pointer and replays the old loop's arithmetic over the
source bytes: one output byte per source byte, two for a sharp s under
upcase or fold, the capitalize flag switch after the first byte. It
writes nothing. It kills the process when the replay reaches a sharp s
with one byte of room left, or when the buffer exceeds `INT_MAX`.
Every other call goes on unchanged. Calls whose source fits even if
every byte took two (every buffer but the first of `rb_str_casemap()`)
and calls without upcase, fold or capitalize flags are let through
without a replay.

`ruby2shell-casemap-step.h` holds that arithmetic. It reads no memory,
and `tests/test_ruby2shell_step.py` builds it on the host and checks
it, over 400,000 random calls, against a statement-for-statement model
of the old loop that records each write past the buffer: the guard
kills exactly the calls the model overruns.

What user space sees: the Ruby process exits on signal 9. Puma in
cluster mode and Unicorn start a new worker; a single-process server
is restarted by its supervisor. The request is lost.

## Packages

| Beefile | Package | `enc/` directory |
|---|---|---|
| `Beefile` | `ruby2shell-casemap-guard-4.0` | `/usr/local/lib/ruby/4.0.0/x86_64-linux/enc` |
| `ruby-3.4.bee` | `ruby2shell-casemap-guard-3.4` | `/usr/local/lib/ruby/3.4.0/x86_64-linux/enc` |
| `ruby-3.3.bee` | `ruby2shell-casemap-guard-3.3` | `/usr/local/lib/ruby/3.3.0/x86_64-linux/enc` |
| `ruby-3.2.bee` | `ruby2shell-casemap-guard-3.2` | `/usr/local/lib/ruby/3.2.0/x86_64-linux/enc` |

These are the directories of a Ruby built from source with the default
prefix and of the official `ruby` images, which build with `make
install` and do not strip, so `case_map` keeps its symbol. Ruby 3.2's
fourteen `case_map()` functions and case flags are those of 3.3 to 4.0
(checked on v3_2_9). Each package has fourteen
programs, one per `.so`; the builder records each file's build ID, so
it loads only with those same files installed. For rbenv, asdf, mise or
rvm, copy a source and point `RUBY2SHELL_ENC_DIR` at
`<prefix>/lib/ruby/<abi>/x86_64-linux/enc`.

`case_map` is a static function: the guard needs it in the `.symtab` of
each `.so`, which builds from source keep. Distribution packages strip
it (the builder then fails with "uprobe function 'case_map' not found");
they also backport fixes, so update the package instead. A Ruby built
with `--with-static-linked-ext` has no `enc/*.so` and is not covered.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`, on a
host where that Ruby is installed:

```sh
shirudo bpm build upatches/ruby2shell-casemap-guard --version 1.0.0 --keys-dir <keys>
shirudo bpm build upatches/ruby2shell-casemap-guard --file upatches/ruby2shell-casemap-guard/ruby-3.4.bee \
	--version 1.0.0 --keys-dir <keys>
shirudo bpm push ruby2shell-casemap-guard-4.0-1.0.0.bee oci://<registry>/<namespace>/ruby2shell-casemap-guard-4.0 --keys-dir <keys>
shirudo bpm tree append ruby2shell-casemap-guard-4.0-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

## Maps

| Map | Counts |
|---|---|
| `hits` | processes killed before a sharp s overran a buffer |
| `overflow` | processes killed for a buffer beyond `INT_MAX` (GHSA-87fm) |
| `unscanned` | processes killed for a replay longer than 64 MiB, see Limits |
| `unchecked` | calls let through because their memory could not be read |
| `activity` | every `case_map()` call of the fourteen encodings |

Each is a per-CPU array of one counter; sum the CPUs.

## Preemption model and cost

The programs keep no state but per-CPU counters and their own stack.
They are sleepable, so a run can be preempted by another on its CPU on
every kernel, and they count with an atomic add. They are right on
every preemption model.

Ruby calls `case_map()` only for strings in these encodings whose cached
coderange is not 7-bit (non-ASCII, or not scanned yet), which is rare:
most strings are UTF-8 or ASCII. A call costs a uprobe hit; a call on
the first buffer of `rb_str_casemap()` replays its source in 128-byte
copies, a few nanoseconds per byte.

## Limits

- The guard turns the overrun into a crash of the process; an attacker
  who can make the application case-map a string in one of these
  encodings can still crash it.
- It refuses without a replay, and so may kill a process the old loop
  would not have overrun, when the replay would exceed 64 MiB of
  source: a string that long, in one of these encodings, with
  non-ASCII bytes, being case-mapped.
- GHSA-87fm also reaches UTF-8 strings, through
  `onigenc_unicode_case_map()` in `libruby`; the guard covers only the
  fourteen encodings above. It needs strings beyond 1 GiB.
- Validated here: the sources compile with shirudo's compiler flags,
  `case_map` resolves to one address in each of the fourteen `.so`
  files of a Ruby 3.3.6 build from source, whose code takes its
  arguments in the standard registers, and the host test above passes.
  Not yet done: a load in shirudo's test VM, where the verifier also
  checks the `bpf_loop()` callback.
- Upgrade Ruby.

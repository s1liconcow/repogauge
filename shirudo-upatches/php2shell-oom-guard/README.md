# php2shell-oom-guard

The `php2shell-oom-guard-*` packages install a upatch that kills a PHP
process with `SIGKILL` when the Zend memory manager gives up on an
allocation, before PHP reports the error or bails out of the request.
That is the path of PHP2Shell, CVE-2026-109618 and CVE-2026-109619:
two remote code execution bugs in PHP before 8.5.12, 8.4.27, 8.3.36
and 8.2.35, back to 5.4. PHP 8.1 and older receive no fix.

## What the bug is and what the guard refuses

Before php-src 506e87fa7d ("Avoid state corruption via bailout"),
`zend_mm_safe_error()` reported "Allowed memory size of … exhausted" or
"Out of memory" with `zend_error_noreturn(E_ERROR, …)` and then
`zend_bailout()`ed out of the allocation, from wherever in the engine
it ran, leaving the structures being built half initialized for the
shutdown code and the next request. A request that drives PHP to its
`memory_limit` at the right place corrupts the worker. The fix unwinds
with an `UnwindExit` exception instead of the bailout.

The upatch's one program is a sleepable uprobe at the entry of
`zend_error_noreturn()`, which every PHP since 7.0 exports. It reads the
error type and the first bytes of the format, and for `E_ERROR` with a
format that starts with `Allowed memory size of ` or `Out of memory` it
sends `SIGKILL` to the process. The worker dies before any code runs on
the half-built state. Every other error goes on.

What user space sees:

- PHP-FPM: the worker exits on signal 9, the master logs it and starts a
  new one, and the web server answers the request with 502.
- mod_php: the Apache child exits on signal 9 and Apache starts another.
- CLI: the script is killed by signal 9 (a shell reports status 137)
  instead of exiting with 255, and prints no "Allowed memory size"
  message.

The guard refuses more than the bug: every out-of-memory error of a
guarded binary becomes a killed process, not only the one at the wrong
place, and the fatal error is not logged. A request that exhausts
`memory_limit` fails either way. A worker that dies this way shows in
the master's log and in the `hits` counter.

## Packages

| Beefile | Package | Binary |
|---|---|---|
| `Beefile` | `php2shell-oom-guard-fpm` | `/usr/local/sbin/php-fpm` |
| `cli.bee` | `php2shell-oom-guard-cli` | `/usr/local/bin/php` |
| `apache.bee` | `php2shell-oom-guard-apache` | `/usr/lib/apache2/modules/libphp.so` |

The paths are those of a PHP built from source with the default prefix
and of the official `php` images. A uprobe names one binary, and the
builder records that binary's GNU build ID in the signed metadata, so
a upatch built against one PHP build loads only where that same file is
installed (`binary_mismatch` elsewhere). For another binary, copy one of
the sources, change `PHP2SHELL_PATH` and add a Beefile for it, for
example `/usr/sbin/php-fpm8.1` of a distribution package. A stripped
binary works: the program needs only the exported symbol.

Distributions backport fixes into their own PHP packages: Ubuntu 24.04's
`8.3.6-0ubuntu0.24.04.14` already carries 506e87fa7d. A fixed PHP does
not need the guard; loaded there, it would still kill a process on an
out-of-memory error that PHP now unwinds cleanly.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`
(`Documentation/start/guides/publish-package.rst`), on a host where the
binary sits at the path the source names:

```sh
shirudo bpm build upatches/php2shell-oom-guard --version 1.0.0 --keys-dir <keys>
shirudo bpm build upatches/php2shell-oom-guard --file upatches/php2shell-oom-guard/cli.bee \
	--version 1.0.0 --keys-dir <keys>
shirudo bpm info php2shell-oom-guard-fpm-1.0.0.bee
shirudo bpm push php2shell-oom-guard-fpm-1.0.0.bee oci://<registry>/<namespace>/php2shell-oom-guard-fpm --keys-dir <keys>
shirudo bpm tree append php2shell-oom-guard-fpm-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

Each tree entry selects x86-64 nodes with `CONFIG_UPROBE_EVENTS`. Add a
`[upatches.tree.selector.labels]` table to load it only on the nodes
that run that PHP build.

## Maps

| Map | Counts |
|---|---|
| `hits` | processes killed before an out-of-memory bailout |
| `activity` | every `zend_error_noreturn()` call (every `zend_error()` call on PHP 7.0, where the two are one function) |

Both are per-CPU arrays of one counter; sum the CPUs.

## Preemption model and cost

The program keeps no state but two per-CPU counters. It is sleepable,
so another run can preempt it on its CPU on any kernel, and it counts
with an atomic add. It is right on every preemption model.

`zend_error_noreturn()` runs on fatal errors only, so the uprobe costs
nothing on the paths a request normally takes. On PHP 7.0 it runs on
every `zend_error()` call, warnings and notices included: one uprobe
hit, a type compare and, for `E_ERROR`, a 13-byte copy.

## Limits

- The guard closes the path of the two CVEs as the fix describes it,
  every bailout out of the memory manager; it does not stop a request
  from exhausting `memory_limit`, which an attacker can still use to
  kill workers one at a time.
- A binary must export `zend_error_noreturn`; every PHP 7 and 8 build
  does, as extensions link against it.
- Validated here: the sources compile with shirudo's compiler flags,
  and `zend_error_noreturn` resolves in Ubuntu 24.04's `php-fpm8.3`,
  `php8.3` and `libphp8.3.so`, all stripped. Not yet done: a load in
  shirudo's test VM and a run against a PHP that exceeds its
  `memory_limit`.
- Upgrade PHP. `2shell-sandbox` contains what a compromised worker does
  next (no other programs, no new `.php` files, private networks only).

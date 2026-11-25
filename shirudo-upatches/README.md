# shirudo upatches for the 2shell advisories

[shirudo](https://github.com/shirudo-dev/shirudo) packages for the
"2shell" advisories of October and November 2026: one guard per bug
where a hook sees the input the fix refuses, and a sandbox that
contains what an attacker does next. Each directory is a package
directory in the layout of shirudo's `upatches/`: copy it there and
build it with `shirudo bpm build` (each `README.md` says how).

| Advisory | IDs | Fixed in | Guard: closes the path | Sandbox: contains |
|---|---|---|---|---|
| PHP2Shell | CVE-2026-109618, CVE-2026-109619 | PHP 8.5.12, 8.4.27, 8.3.36, 8.2.35 | [`php2shell-oom-guard`](php2shell-oom-guard/): kills PHP before a bailout out of an allocation | [`2shell-sandbox`](2shell-sandbox/) `apache`, `php-fpm` |
| Node2Shell, HTTP2Shell | CVE-2026-111497 | Node.js 22.23.4, 24.22.1, 26.12.1 | [`node2shell-brotli-guard`](node2shell-brotli-guard/): kills Node.js before a Brotli block switch on a tree of one block type | none, see its README |
| Ruby2Shell | CVE-2026-132825 (GHSA-34m4-m9fh-gqp5); GHSA-87fm-7w84-j2px | Ruby 3.3.14, 3.4.13, 4.0.10 | [`ruby2shell-casemap-guard`](ruby2shell-casemap-guard/): kills Ruby before a case mapping overruns its buffer | none, see its README |
| NGINX2Shell (NGINX2Root) | CVE-2026-123007 | nginx 1.31.9, 1.30.7 | [`nginx2shell-h2-window-guard`](nginx2shell-h2-window-guard/): kills a worker whose HTTP/2 window passes the maximum | [`2shell-sandbox`](2shell-sandbox/) `nginx` |
| NGINX2Root | CVE-2026-123633 | nginx 1.31.9, 1.30.7 | [`nginx2root-h2-drain-guard`](nginx2root-h2-drain-guard/): kills a worker before a connection drain closes HTTP/2 streams | [`2shell-sandbox`](2shell-sandbox/) `nginx` |
| NGINX2Root, the other three | CVE-2026-123634, CVE-2026-121810, CVE-2026-113425 | nginx 1.31.9, 1.30.7 | none | [`2shell-sandbox`](2shell-sandbox/) `nginx` |
| NGINX2Root, kernel half | SCTP use-after-free, no CVE yet | not yet | [`nginx2root-sctp-block`](nginx2root-sctp-block/): refuses SCTP sockets to user space | |
| Plex2Shell | CVE-2026-96652, CVE-2026-96654, CVE-2026-96655, CVE-2026-96656 | Plex Media Server 1.43.3.10861 | none: closed source | [`2shell-sandbox`](2shell-sandbox/) `plex` |
| Java2Shell, Java4Shell | CVE-2026-114938 | OpenJDK 8393212, 17 November 2026 | [`java2shell-base64-guard`](java2shell-base64-guard/): a Java agent that refuses the Base64 input the AVX-512 stub mishandles | |

React2Shell (CVE-2025-55182, December 2025) predates this wave and is
not covered.

## Coverage of each CVE

| CVE | Guard | Checked against | Not covered |
|---|---|---|---|
| CVE-2026-114938 | `java2shell-base64-guard` | OpenJDK 17.0.21, 21.0.13, 25.0.5: the agent installs at start and by attach, and changes the outcome of no input but the refused shape; OpenJDK 8 and 11 start with it and are left alone | JVMs the agent does not reach |
| CVE-2026-109618, CVE-2026-109619 | `php2shell-oom-guard` | the three bailout formats of the fix's diff, PHP 7.0 to 8.5; `zend_error_noreturn` in Ubuntu 24.04's stripped `php-fpm8.3`, `php8.3`, `libphp8.3.so` | none of the bailout paths; a binary path the sources do not name needs a copy of a source |
| CVE-2026-111497 | `node2shell-brotli-guard` | official v20.20.2 (end of life, never fixed), v22.23.3, v24.22.0, v26.12.0 | distribution builds that link a stripped system `libbrotlidec`: Ubuntu fixed its `libbrotli1` (1.1.0-2ubuntu0.1) |
| CVE-2026-123007 | `nginx2shell-h2-window-guard` | Ubuntu 24.04's stripped `nginx` 1.24.0 (offset `0x60`), synthetic code | none |
| CVE-2026-123633 | `nginx2root-h2-drain-guard` | Ubuntu 24.04's stripped `nginx` 1.24.0; drain path read in 1.24.0 and 1.31.8 | none |
| CVE-2026-132825 | `ruby2shell-casemap-guard` | Ruby 3.2 (end of life), 3.3, 3.4, 4.0 sources; a Ruby 3.3.6 build; 400,000 calls against a model of the old loop | distribution Ruby, stripped (distributions backport); `--with-static-linked-ext` builds |

Ubuntu 24.04's own `php8.3`, `nginx` and `libbrotli1` packages already
carry the fixes for their CVEs; the guards matter for builds from
source, the official container images and end-of-life releases.

## How the guards work

The PHP, Node.js, Ruby and nginx bugs are in user space. Each BPF guard
is a uprobe on a function of the vulnerable binary that the upstream fix
changed or that its fixed check sits behind. It reads the arguments,
tests them for the input the fix refuses and, for that input, kills the
process with `bpf_send_signal(SIGKILL)` before the function body runs.
A uprobe cannot change a function's return value, and shirudo's
lockdown refuses programs that write user memory
(`bpf_probe_write_user()`), so a guard cannot make the call fail the way
the fixed code does: it turns the memory corruption into a crash of one
process, which a supervisor restarts. An attacker can still crash it.
The Java guard is a Java agent instead: it puts a check at the start of
the decoder method that calls the vulnerable stub and throws the
exception the decoder would throw for that input anyway. Every README
says what the guard refuses, what user space sees, what it costs and
what it does not cover.

A uprobe names one binary, and `shirudo build` records that binary's
GNU build ID in the signed metadata, so a guard loads only where the
same file is installed. Build each package on an operator host that has
the binary the source names, at that path; the READMEs say how to point
a source at another path. The Node.js and nginx guards also read a
struct offset of that binary from its code at `bpm build`, with the
`.rodata` generator of their Beefile, and fail the build if the code
does not show it.

The guards are written for x86-64 and for every preemption model:
they keep no state that two runs share but per-CPU counters, which
they add to atomically.

## Checks

```sh
SHIRUDO_SRC=<shirudo checkout> shirudo-upatches/check.sh
```

compiles every source the way `shirudo build` does
(`clang -target bpf -mcpu=v4 -fno-jump-tables -O2 -g`, here with
`-Wall -Werror` on top) against the checkout's libbpf, `bpf/lib` and
`vmlinux.h`, and runs `tests/`:

- `test_ruby2shell_step.py` checks the Ruby guard's decision against a
  model of the old loop over 400,000 random calls.
- `test_gen_offsets.py` checks both offset generators on synthetic code,
  and on real binaries when `NODE2SHELL_NODE` and `NGINX2SHELL_NGINX`
  name them.
- `test_java2shell_guard.py` builds the Java agent and runs
  `tests/java2shell/Probe.java` with and without it, in the JDKs
  `JAVA2SHELL_JDKS` lists or the `java` on `PATH`, and attaches it to a
  running JVM.

Done so far, beyond those: every uprobe of every package resolves to one
function, in a binary with a build ID, in the official Node.js v20.20.2,
v22.23.3, v24.22.0 and v26.12.0 builds, in Ubuntu 24.04's `nginx` 1.24.0,
`php-fpm8.3`, `php8.3` and `libphp8.3.so`, and in a Ruby 3.3.6 built
from source: 46 probes. The offset generators read from those binaries what their
disassembly shows. Not done: signing, `bpm build` and a load in
shirudo's test VM, where the kernel's verifier runs and the guards meet
a live process. Each README lists what remains for its package.

## Sources

- PHP: [php-src 506e87fa7d](https://github.com/php/php-src/commit/506e87fa7dcdd306975e5b8f8265ee03527aacbf), GHSA-w8hw-w9fx-j79w, GHSA-6wmf-4rv5-p3rp
- Node.js: [October 2026 security releases](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases), [google/brotli 0ed4268](https://github.com/google/brotli/commit/0ed4268a4401abcb4472096340477cf4af7d1a2b)
- Ruby: [Security advisories: multiple vulnerabilities in Regexp and String case conversion](https://www.ruby-lang.org/en/news/2026/11/18/multiple-vulnerabilities-in-regexp-and-string/), [ruby/ruby d582f8f](https://github.com/ruby/ruby/commit/d582f8f13d700d8e39714d3a8f11eb4bd2bc69f0), [ruby/ruby bdff3c5d66dd](https://github.com/ruby/ruby/commit/bdff3c5d66dd40b591f2dcda4d4694dcfabf8db4)
- nginx: [nginx e27f5fa8](https://github.com/nginx/nginx/commit/e27f5fa84fe6e98505e70bc543b748747dad0a3b), [nginx 2df48a50](https://github.com/nginx/nginx/commit/2df48a501d0fb8212b7d39dffeeff5876332ba59), [Help Net Security on NGINX2Root](https://www.helpnetsecurity.com/2026/11/19/nginx2root-nginx-vulnerabilities/), [Tenable FAQ](https://www.tenable.com/blog/nginx2root-cve-2026-123007-frequently-asked-questions-about-five-critical-nginx)
- Plex: [SonicWall on Plex2Shell](https://www.sonicwall.com/blog/plex2shell-plex-media-server-exploit-chain-ssrf-to-arbitrary-code-execution), [Hive Pro advisory](https://www.hivepro.com/threat-advisory/plex2shell-exploit-chain-actively-exploited-to-hijack-plex-media-servers)
- Java: OpenJDK 8393212 ([openjdk/jdk21u-dev 8e115fb5](https://github.com/openjdk/jdk21u-dev/commit/8e115fb5c7c5e2b35f0aa86353ce3f84b49e7319)), [Socura on CVE-2026-114938](https://socura.co.uk/threat-alerts/java2shell-cve-2026-114938)

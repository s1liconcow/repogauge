# 2shell-sandbox

The `2shell-sandbox` package installs shirudo's `app-sandbox` upatch
with sandboxes for the servers of the 2shell advisories: Apache httpd
and PHP-FPM (PHP2Shell), nginx (NGINX2Root) and Plex Media Server
(Plex2Shell). Each of those advisories ends with code an attacker runs
inside the server, which then starts a shell, a miner or a proxy, or
leaves a file behind that the server or a backend runs later. A task in
one of these sandboxes starts no program but its own, and creates,
renames or opens for writing no file whose name matches the sandbox's
patterns.

The sandboxes contain the advisories rather than close them: the
attacker's code still runs inside the server. The guards of this tree
close what they can, and the servers need upgrading either way. The
tree entry names every advisory the sandboxes contain in `fixes`, as
`app-sandbox` does for PHP2Shell, so that a backend that reads the tree
counts a node that loaded it.

## Sources

`2shell-sandbox.bpf.c` and `gen_policy.py` are `app-sandbox.bpf.c` and
`gen_policy.py` of shirudo's `upatches/app-sandbox/`, unchanged, as of
shirudo commit 6514073:

```
9b011761fc9661b5d719744d3617cf84a11c872ff56980fa01ee435a3989dcc7  2shell-sandbox.bpf.c
9ba2ba4c0d6050e62a10190d8b8f35a1502eaecaa17688999247d85f3ae3fbca  gen_policy.py
```

Only the `Beefile` and `sandbox.toml` are new, and the program, the
rules' format and every key are those of `app-sandbox`: its `README.md`
is the reference.

The `apache` and `php-fpm` policies keep the IDs, bits and rules of
`app-sandbox`, so this package replaces `app-sandbox` on a node: load
one or the other, not both.

## The sandboxes

| Sandbox | Bit | Labels | Starts | Writes no | Network |
|---|---|---|---|---|---|
| `apache` | `0x1` | `/usr/sbin/apache2`, `/usr/sbin/httpd`, `/usr/local/apache2/bin/httpd` | Apache | `*.php` | private networks |
| `php-fpm` | `0x2` | `/usr/sbin/php-fpm*`, `/usr/local/sbin/php-fpm` | PHP-FPM | `*.php`, `*.phar`, `*.phtml`, `*.pht`, `*.php?` | private networks |
| `nginx` | `0x4` | `/usr/sbin/nginx`, `/usr/local/nginx/sbin/nginx`, `/usr/local/openresty/nginx/sbin/nginx` | nginx | the PHP names, `*.so`, `*.sh`, `*.cgi` | any |
| `plex` | `0x8` | every `/usr/lib/plexmediaserver/Plex*` | the Plex programs | `*.so`, `*.py`, `*.sh` | any |

"Starts" is the `exec` rule: a task executes only files with its own
label. "Writes no" covers `create`, `rename` and `write`.

- **nginx.** A worker starts nothing; the master re-executes nginx on a
  binary upgrade, which the label allows. The patterns keep a
  compromised worker from writing a page a PHP or CGI backend would run,
  a shared object the dynamic loader would load, or a script. There is
  no network rule: nginx proxies to upstreams, resolves names and
  fetches OCSP responses wherever its configuration says. Add one, as
  the `apache` sandbox has, for a server whose upstreams are all on
  private networks.
- **plex.** Plex2Shell's last steps write a codec library that the
  transcoder then loads, and start miners and proxies. The pattern
  labels every `Plex …` program (Plex Media Server, Plex Transcoder,
  Plex Media Scanner, Plex Script Host, Plex Tuner Service and the
  others), so the server starts only those. It writes no `.so`: **codec
  downloads fail while the package is loaded**, and codecs already
  downloaded keep working. Unload the package to update codecs, after
  the server is upgraded to 1.43.3.10861 or later. There is no network
  rule: the server talks to plex.tv and metadata providers. Programs
  of the Plex directory whose name does not start with `Plex`, such as
  a crash uploader, cannot be started by the server; watch the server's
  log for `EACCES` after the first load.

No sandbox is shipped for Node.js (Node2Shell), Ruby (Ruby2Shell) or
Java (Java2Shell). A sandbox labels an executable, and `node`, `ruby`
and `java` run far more than the server, `npm`, `bundle` and build
tools among them, which `exec = []` and the patterns would break. To
sandbox such a server, give it a copy of the interpreter of its own (a
copy, not a hard link: the label is on the inode), add a policy for
that path and start the server with it.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`:

```sh
shirudo bpm build upatches/2shell-sandbox --version 1.0.0 --keys-dir <keys>
shirudo bpm info data 2shell-sandbox-1.0.0.bee upatches/2shell-sandbox.upatch
shirudo bpm push 2shell-sandbox-1.0.0.bee oci://<registry>/<namespace>/2shell-sandbox --keys-dir <keys>
shirudo bpm tree append 2shell-sandbox-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

The load labels the paths that exist on the node; a task enters its
sandbox at its next `execve()` of a labeled file, so restart the
servers after the first load. On a node without them it labels nothing
and restricts nothing. `app-sandbox`'s `README.md` describes the rules,
`--rodata-input sandbox=<file>` and `shirudo bpm config update` for
other rules, and the limits, which all hold here.

## Kernels and cost

As `app-sandbox`: the programs keep no state of their own, so they are
right on every preemption model; a task in no sandbox pays one
task-storage lookup at each hook, and a task in a sandbox a scan of the
rules on top, here four exec rules, eight networks and nine patterns.

## Validated here

`gen_policy.py` accepts the Beefile and `sandbox.toml` and writes a
10,792-byte `sandbox_policy`, the size of the struct in the program,
with four sandboxes, four exec rules, eight networks and nine patterns,
and the program compiles with shirudo's compiler flags. Not yet done:
a load in shirudo's test VM, and a Plex Media Server run under the
`plex` sandbox, which this tree has not tried.

# nginx2shell-h2-window-guard

The `nginx2shell-h2-window-guard` package installs a upatch that kills
an nginx worker with `SIGKILL` when the flow-control window of one of
its HTTP/2 connections passes `NGX_HTTP_V2_MAX_WINDOW`, before the
worker uses that window again. That is NGINX2Shell, CVE-2026-123007,
the HTTP/2 half of the NGINX2Root set: an integer underflow in HTTP/2
flow control in every nginx with HTTP/2, 1.9.5 to 1.31.8, fixed in
1.31.9 and 1.30.7. It needs a server with `http2` enabled.

## What the bug is and what the guard refuses

When a stream ends, `ngx_http_v2_filter_cleanup()` refunds the payload
of its unsent DATA frames to the connection's send window. If the
client raised the window with WINDOW_UPDATE after those frames were
queued, the refund took `h2c->send_window` past
`NGX_HTTP_V2_MAX_WINDOW`; `NGX_HTTP_V2_MAX_WINDOW - h2c->send_window`
then wraps in the next WINDOW_UPDATE check, and
`ngx_http_v2_send_chain()` sizes frames from a broken window. The fix
(nginx e27f5fa8) counts the queued frames in `h2c->send_queued` and
holds `send_window + send_queued <= NGX_HTTP_V2_MAX_WINDOW` when it
validates a WINDOW_UPDATE, so the refund cannot take the window past
the maximum.

The refund runs among the request cleanups `ngx_http_v2_close_stream()`
runs, and `ngx_http_v2_close_stream()` is exported by every nginx linked
with `-Wl,-E`, which `configure` adds for dynamic modules wherever the
linker has it, stripped or not. The upatch has a uprobe at its entry,
which notes the stream's connection, and a uretprobe at its return,
which reads the connection's send window: above
`NGX_HTTP_V2_MAX_WINDOW` it sends `SIGKILL` to the worker. The window
is never above the maximum on a fixed nginx, and on a vulnerable one
only in this bug, so the guard refuses nothing else and is harmless on
a fixed build.

What user space sees: the worker exits on signal 9, the master logs
"worker process … exited on signal 9" and starts a new one, and the
connections of the killed worker, all of them, are closed. That is a
denial of service an attacker can repeat, in place of memory corruption
in the worker.

`http2 off` (or, before 1.25.1, dropping `http2` from the `listen`
directives) closes the bug as well, as F5's advisory says, at the cost
of HTTP/2.

## Binary and offset

The source names `/usr/sbin/nginx`, the path of the nginx.org packages,
of Debian, Ubuntu and RHEL and of the official `nginx` image. The
builder records that binary's GNU build ID, so the upatch loads only
where that same file is installed (`binary_mismatch` elsewhere): build
one package per nginx build you run and select the nodes with labels.

The guard reads `ngx_http_v2_connection_t.send_window`, whose offset
moves between releases and with distribution patches: `0x50` in 1.18 to
1.31.6 upstream, `0x48` in 1.31.7 and 1.31.8, `0x60` in Ubuntu 24.04's
1.24.0, which carries backported fields. `gen_offsets.py`, the `.rodata`
generator of the Beefile, reads it at `bpm build` from the binary the
source names: `ngx_http_v2_init()` sets `send_window` to 65535,
`recv_window` to `NGX_HTTP_V2_MAX_WINDOW` and `init_window` to 65535,
three consecutive fields, and the generator looks for those three
stores off one register. It also checks that `ngx_http_v2_close_stream()`
loads `stream->connection` from offset 8. Anything else fails the
build.

If a compiler stored those fields another way, find the offset by hand
and pass it to the generator in the Beefile:

```sh
objdump -d --disassemble=ngx_http_v2_init /usr/sbin/nginx | grep -E '\$0xffff,|\$0x7fffffff,'
#   movq $0xffff,0x60(%rax)  movq $0x7fffffff,0x68(%rax)  movq $0xffff,0x70(%rax)
```

```toml
generator = ["python3", "gen_offsets.py", "--send-window", "0x60"]
update = ["python3", "gen_offsets.py", "--send-window", "0x60"]
```

A package built without the generator holds 0 and checks nothing.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`, on a
host where the nginx build sits at `/usr/sbin/nginx`:

```sh
shirudo bpm build upatches/nginx2shell-h2-window-guard --version 1.0.0 --keys-dir <keys>
shirudo bpm info data nginx2shell-h2-window-guard-1.0.0.bee upatches/nginx2shell-h2-window-guard.upatch
shirudo bpm push nginx2shell-h2-window-guard-1.0.0.bee oci://<registry>/<namespace>/nginx2shell-h2-window-guard --keys-dir <keys>
shirudo bpm tree append nginx2shell-h2-window-guard-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

## Maps

| Map | Holds |
|---|---|
| `closing` | the connection of the stream each thread is closing, between entry and return (LRU, 1024) |
| `hits` | workers killed with a send window above the maximum |
| `activity` | every stream close |
| `unchecked` | closes not checked: no offset, an entry missed, memory not readable |

The counters are per-CPU arrays of one counter; sum the CPUs.

## Preemption model and cost

`closing` is keyed by thread, and an nginx worker runs HTTP/2 on one
thread, so no two runs share an entry; the counters are per-CPU and
take an atomic add, as a run can be preempted by another on its CPU
under preemptible RCU. The programs are right on every preemption
model.

Each HTTP/2 stream close costs a uprobe and a uretprobe hit, a few
microseconds together, one map update and lookup and two 8-byte reads:
on the order of 3% of a CPU at 10,000 HTTP/2 requests a second.

## Limits

- The guard turns the corruption into a crash of a worker; an HTTP/2
  client can still crash workers.
- It covers CVE-2026-123007 only. `nginx2root-h2-drain-guard` covers
  the HTTP/2 upstream use-after-free of the set, CVE-2026-123633. The
  resolver CVE-2026-123634, HTTP/3 CVE-2026-121810 and the mail proxy
  CVE-2026-113425 have no guard here; `2shell-sandbox` contains what a
  compromised worker does next, and `nginx2root-sctp-block` the kernel
  bug the set uses to reach root.
- A nested `ngx_http_v2_close_stream()` on one thread overwrites the
  entry of the outer call, whose return then goes unchecked (counted in
  `unchecked`); nginx does not close a stream from inside the close of
  another.
- Validated here: the source compiles with shirudo's compiler flags,
  `ngx_http_v2_close_stream` resolves in Ubuntu 24.04's stripped
  `/usr/sbin/nginx`, and `gen_offsets.py` reads `0x60` from it, which
  its disassembly confirms (the WINDOW_UPDATE handler stores the new
  window at `0x60(%rbx)`); the generator's decoder is tested on
  synthetic code with other offsets and base registers. The upstream
  offsets above come from the struct definitions of each release, not
  from built binaries. Not yet done: a load in shirudo's test VM.
- Upgrade nginx.

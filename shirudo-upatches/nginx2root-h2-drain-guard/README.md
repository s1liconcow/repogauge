# nginx2root-h2-drain-guard

The `nginx2root-h2-drain-guard` package installs a upatch that kills an
nginx worker with `SIGKILL` when a connection drain is about to close
the streams of an HTTP/2 connection, before anything of them is freed.
That is the path of CVE-2026-123633, the HTTP/2 use-after-free of the
NGINX2Root set, in nginx 1.19.7 to 1.31.8 with HTTP/2 enabled; nginx
1.31.9 and 1.30.7 fix it. It matters most where HTTP/2 requests are
proxied to upstreams.

## What the bug is and what the guard refuses

Since nginx 1.19.7 (76672e65), an HTTP/2 connection that has read part
of a frame and has no streams is put on the queue of reusable
connections, and a stream created on it afterwards did not take it off
the queue. A worker that runs out of `worker_connections` drains that
queue: `ngx_drain_connections()` sets `c->close` and calls each
connection's read handler. For an HTTP/2 connection that has streams,
the handler sends a GOAWAY, and if that send fails,
`ngx_http_v2_finalize_connection()` closes the streams and then the
connection, freeing their memory, while code of one of those streams
may be up the stack: a stream that was connecting to an upstream and
asked for a connection is what triggered the drain. The fix (nginx
2df48a50, "HTTP/2: reset reusable connection status on stream
creation") takes a connection off the queue when it creates a stream,
so a drain only ever finds HTTP/2 connections without streams.

`ngx_drain_connections()` has one caller, `ngx_get_connection()`, and
the streams of a connection are closed only through
`ngx_http_v2_close_stream()`, which frees the stream's request first;
both functions are exported by every nginx linked with `-Wl,-E`, which
`configure` adds for dynamic modules wherever the linker has it,
stripped or not. The upatch has three programs:

| Program | At | Does |
|---|---|---|
| `nginx2root_get_connection` | entry of `ngx_get_connection()` | notes that the thread is in it, with its stack pointer |
| `nginx2root_got_connection` | return of `ngx_get_connection()` | removes the note |
| `nginx2root_close_stream` | entry of `ngx_http_v2_close_stream()` | kills the worker if the thread is inside `ngx_get_connection()` |

On a fixed nginx a drain never closes an HTTP/2 stream, so the guard
never fires there; on a vulnerable one it fires only in this bug, at
the first stream the drain closes. A drain whose GOAWAY goes out closes
no stream, frees nothing and goes on. The guard reads no struct field:
it needs no offset and no `.rodata` generator.

What user space sees: the worker exits on signal 9, the master logs it
and starts a new one, and every connection of that worker is closed. A
client that can bring a worker to run out of connections can repeat
that; it can no longer free memory under the worker.

`http2 off` closes the bug as well, at the cost of HTTP/2.

## Build and publish

The source names `/usr/sbin/nginx`; the builder records that binary's
GNU build ID, so the upatch loads only where that same file is
installed. Build in a shirudo checkout, with the directory under
`upatches/`, on a host where the nginx build sits at that path:

```sh
shirudo bpm build upatches/nginx2root-h2-drain-guard --version 1.0.0 --keys-dir <keys>
shirudo bpm push nginx2root-h2-drain-guard-1.0.0.bee oci://<registry>/<namespace>/nginx2root-h2-drain-guard --keys-dir <keys>
shirudo bpm tree append nginx2root-h2-drain-guard-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

It loads beside `nginx2shell-h2-window-guard`; both put a uprobe on
`ngx_http_v2_close_stream()`, which the kernel runs one after the other.

## Maps

| Map | Holds |
|---|---|
| `getting` | per thread inside `ngx_get_connection()`: the stack pointer of its outermost entry and the depth (LRU, 1024) |
| `hits` | workers killed when a drain was about to close an HTTP/2 stream |
| `activity` | every HTTP/2 stream close |
| `stale` | notes of a call whose return the uretprobe missed, dropped |

The counters are per-CPU arrays of one counter; sum the CPUs.

## Preemption model and cost

`getting` is keyed by thread, and an nginx worker runs its event loop
on one thread, so each entry has one writer; the counters are per-CPU
and take an atomic add, as a run can be preempted by another on its CPU
under preemptible RCU. The programs are right on every preemption
model.

`ngx_get_connection()` runs once per accepted connection and once per
upstream connection: a uprobe and a uretprobe hit, a few microseconds,
and a map update. Each HTTP/2 stream close adds a uprobe hit and a map
lookup.

## Limits

- The guard turns the use-after-free into a crash of a worker.
- A uretprobe the kernel does not run (it gives up on return probes
  nested too deep, or when it cannot allocate one) leaves a note
  behind. A later stream close on that thread higher on the stack than
  the note's stack pointer drops it and counts it in `stale`; one deeper
  on the stack would be taken for a drain and kill the worker. nginx
  does not nest `ngx_get_connection()`, so this takes a kernel failure.
- Validated here: the source compiles with shirudo's compiler flags, and
  `ngx_get_connection` and `ngx_http_v2_close_stream` resolve in Ubuntu
  24.04's stripped `/usr/sbin/nginx`, where `ngx_drain_connections()`
  is inlined into `ngx_get_connection()`. The analysis of the drain path
  is from the nginx 1.24.0 and 1.31.8 sources. Not yet done: a load in
  shirudo's test VM and a worker brought to drain its connections.
- Upgrade nginx.

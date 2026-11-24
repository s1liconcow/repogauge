# nginx2root-sctp-block

The `nginx2root-sctp-block` package installs a upatch that refuses
every SCTP socket user space asks for, with `EPERM`. The NGINX2Root set
chains the nginx HTTP/2 bug, CVE-2026-123007, which runs code as an
nginx worker, with a use-after-free in the kernel's SCTP code, which
takes that worker to root. The kernel bug has no CVE and no fix yet;
a process reaches it through an SCTP socket of its own. Refusing the
socket takes the SCTP code out of reach of every local process.

## What it refuses

Two `lsm` programs, for `AF_INET` and `AF_INET6` sockets that user space
creates:

| Hook | Refuses |
|---|---|
| `lsm/socket_create` | `socket(…, …, IPPROTO_SCTP)`, before the kernel would load `sctp.ko` for it |
| `lsm/socket_post_create` | a socket whose protocol the kernel chose as SCTP, as for `SOCK_SEQPACKET` with protocol 0 once `sctp.ko` is loaded |

`socket()` fails with `EPERM`. Sockets the kernel opens for itself
(`kern` set), as DLM does for cluster links over SCTP, are left alone.
Everything else, TCP, UDP and every other family, goes on.

On a node that uses SCTP from user space (telecom stacks, some cluster
software, `iperf3 --sctp`) this breaks that use; such a node should not
take the package, or should take it only when its SCTP is not needed.
Blacklisting the `sctp` module does the same where SCTP is a module and
nothing has loaded it; this upatch also covers a built-in SCTP and an
already loaded module.

The package's tree entry has no `fixes`: the kernel bug has no ID yet.
It selects nodes whose kernel has `CONFIG_IP_SCTP`, and loads on every
architecture.

## Build and publish

```sh
shirudo bpm build upatches/nginx2root-sctp-block --version 1.0.0 --keys-dir <keys>
shirudo bpm push nginx2root-sctp-block-1.0.0.bee oci://<registry>/<namespace>/nginx2root-sctp-block --keys-dir <keys>
shirudo bpm tree append nginx2root-sctp-block-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

## Kernels and cost

The programs keep no state and read only their arguments and
`sock->sk->sk_protocol` through CO-RE, so they are right on every
preemption model and every kernel with the BPF LSM. They run once per
`socket()` call: two compares, and one read for an inet socket.

## Limits

- A process that holds an SCTP socket from before the load keeps it.
- Validated here: the source compiles with shirudo's compiler flags
  against shirudo's `vmlinux.h`. Not yet done: a load in shirudo's test
  VM and a `socket(AF_INET, SOCK_STREAM, IPPROTO_SCTP)` that fails with
  `EPERM`.
- Remove the package once the SCTP fix is in the node's kernel.

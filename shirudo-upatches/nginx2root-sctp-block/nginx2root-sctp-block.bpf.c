// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * NGINX2Root: Xint chains the nginx HTTP/2 bug, CVE-2026-123007, which
 * runs code as an nginx worker, with a use-after-free in the kernel's
 * SCTP code that takes the worker to root. The SCTP bug has no CVE and
 * no fix yet. A worker reaches it through an SCTP socket of its own.
 *
 * This refuses every SCTP socket user space asks for, with EPERM, which
 * takes the SCTP code out of reach of a local process. socket_create
 * refuses IPPROTO_SCTP by number before the kernel would load sctp.ko
 * for it. socket_post_create refuses the socket whatever protocol
 * number it was asked for, from the protocol the kernel chose: a
 * SOCK_SEQPACKET socket of protocol 0 is an SCTP one once sctp.ko is
 * loaded. Sockets the kernel opens for itself (kern != 0), as DLM does
 * for SCTP cluster links, are left alone.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define AF_INET		2
#define AF_INET6	10
#define EPERM		1

char _license[] SEC("license") = "Dual BSD/GPL";

static __always_inline bool inet_family(int family)
{
	return family == AF_INET || family == AF_INET6;
}

SEC("lsm/socket_create")
int BPF_PROG(nginx2root_sctp_create, int family, int type, int protocol,
	     int kern)
{
	if (kern || !inet_family(family))
		return 0;
	if (protocol == IPPROTO_SCTP)
		return -EPERM;
	return 0;
}

SEC("lsm/socket_post_create")
int BPF_PROG(nginx2root_sctp_post_create, struct socket *sock, int family,
	     int type, int protocol, int kern)
{
	if (kern || !inet_family(family))
		return 0;
	if (BPF_CORE_READ(sock, sk, sk_protocol) == IPPROTO_SCTP)
		return -EPERM;
	return 0;
}

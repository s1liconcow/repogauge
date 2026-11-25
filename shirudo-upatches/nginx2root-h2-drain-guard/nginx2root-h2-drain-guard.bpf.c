// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * NGINX2Root, CVE-2026-123633: since nginx 1.19.7 (76672e65, "HTTP/2:
 * reuse of connections with incomplete frames") an HTTP/2 connection
 * that has read part of a frame and has no streams is put on the
 * reusable queue, and a stream created afterwards did not take it off.
 * When a worker runs out of connections, ngx_drain_connections() closes
 * reusable ones: it sets c->close and calls the connection's read
 * handler. For an HTTP/2 connection that has streams, that sends a
 * GOAWAY; if the send fails, ngx_http_v2_finalize_connection() closes
 * the streams and the connection and frees their memory, while code of
 * one of those streams may be up the stack, for example the one that
 * was connecting to an upstream and asked for the connection. The fix
 * (nginx 2df48a50, "HTTP/2: reset reusable connection status on stream
 * creation", in nginx 1.31.9 and 1.30.7) takes a connection off the
 * queue when it creates a stream, so a drain only ever finds HTTP/2
 * connections without streams.
 *
 * ngx_drain_connections() runs only inside ngx_get_connection(), and
 * a connection's streams are closed only through
 * ngx_http_v2_close_stream(), which frees the stream's request before
 * the connection can go; both are exported by every nginx linked with
 * -Wl,-E. These programs note on each thread that it runs
 * ngx_get_connection(), from its entry to its return, and kill the
 * worker with SIGKILL at the entry of ngx_http_v2_close_stream() inside
 * it: a stream closed by a drain, which a fixed nginx never does, before
 * anything of it is freed. A drain that only sends the GOAWAY frees
 * nothing and goes on.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#ifndef __TARGET_ARCH_x86
#define __TARGET_ARCH_x86
#endif
#include <bpf/bpf_tracing.h>

#define NGINX2ROOT_PATH		"/usr/sbin/nginx"

char _license[] SEC("license") = "Dual BSD/GPL";

#define SIGKILL			9

/* A thread inside ngx_get_connection(): its outermost entry. */
struct in_get_connection {
	__u64	sp;		/* the stack pointer at that entry */
	__u32	depth;		/* calls of the thread not returned yet */
	__u32	reserved;
};

/*
 * Keyed by thread. A worker runs its event loop, and so every
 * ngx_get_connection(), on one thread, so an entry has one writer;
 * LRU, so that an entry a killed worker leaves behind goes.
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 1024);
	__type(key, __u64);
	__type(value, struct in_get_connection);
} getting SEC(".maps");

/* Workers killed when a drain was about to close an HTTP/2 stream. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} hits SEC(".maps");

/* Every HTTP/2 stream close, the denominator of hits. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} activity SEC(".maps");

/* Entries of a call whose return the uretprobe missed, dropped. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} stale SEC(".maps");

static __always_inline void count_event(void *map)
{
	__u32 zero = 0;
	__u64 *count = bpf_map_lookup_elem(map, &zero);

	/* With preemptible RCU another run can interleave on this CPU. */
	if (count)
		__sync_fetch_and_add(count, 1);
}

SEC("uprobe/" NGINX2ROOT_PATH ":ngx_get_connection")
int BPF_UPROBE(nginx2root_get_connection)
{
	__u64 id = bpf_get_current_pid_tgid();
	struct in_get_connection *in, entry = {};

	in = bpf_map_lookup_elem(&getting, &id);
	if (in) {
		in->depth++;
		return 0;
	}
	entry.sp = PT_REGS_SP(ctx);
	entry.depth = 1;
	bpf_map_update_elem(&getting, &id, &entry, BPF_ANY);
	return 0;
}

SEC("uretprobe/" NGINX2ROOT_PATH ":ngx_get_connection")
int BPF_URETPROBE(nginx2root_got_connection)
{
	__u64 id = bpf_get_current_pid_tgid();
	struct in_get_connection *in;

	in = bpf_map_lookup_elem(&getting, &id);
	if (!in)
		return 0;
	if (in->depth > 1) {
		in->depth--;
		return 0;
	}
	bpf_map_delete_elem(&getting, &id);
	return 0;
}

SEC("uprobe/" NGINX2ROOT_PATH ":ngx_http_v2_close_stream")
int BPF_UPROBE(nginx2root_close_stream)
{
	__u64 id = bpf_get_current_pid_tgid();
	struct in_get_connection *in;

	count_event(&activity);
	in = bpf_map_lookup_elem(&getting, &id);
	if (!in)
		return 0;
	/*
	 * Every function ngx_get_connection() calls runs below its entry's
	 * stack pointer. An entry the thread is not below is one whose
	 * return the uretprobe missed: drop it rather than kill.
	 */
	if (PT_REGS_SP(ctx) >= in->sp) {
		bpf_map_delete_elem(&getting, &id);
		count_event(&stale);
		return 0;
	}
	count_event(&hits);
	bpf_send_signal(SIGKILL);
	return 0;
}

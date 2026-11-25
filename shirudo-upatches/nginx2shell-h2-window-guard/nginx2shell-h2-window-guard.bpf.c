// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * NGINX2Shell (the HTTP/2 half of NGINX2Root), CVE-2026-123007: when an
 * HTTP/2 stream is torn down, ngx_http_v2_filter_cleanup() refunds the
 * payload of its unsent DATA frames to the connection's flow-control
 * window. A client that raised the window with WINDOW_UPDATE after the
 * frames were queued could push h2c->send_window past
 * NGX_HTTP_V2_MAX_WINDOW that way; ngx_http_v2_state_window_update()
 * then computes NGX_HTTP_V2_MAX_WINDOW - h2c->send_window, which wraps,
 * and ngx_http_v2_send_chain() sizes frames from a broken window.
 *
 * The fix (nginx e27f5fa8, "HTTP/2: account for queued frames when
 * validating WINDOW_UPDATE", in nginx 1.31.9 and 1.30.7) counts the
 * queued frames in h2c->send_queued and holds
 * send_window + send_queued <= NGX_HTTP_V2_MAX_WINDOW, so the refund
 * never takes send_window past the maximum.
 *
 * The refund runs among the request cleanups that
 * ngx_http_v2_close_stream() runs through ngx_http_free_request(), and
 * ngx_http_v2_close_stream() is exported by every nginx linked with
 * -Wl,-E, which configure adds wherever the linker has it, for dynamic
 * modules. These programs note the stream's connection at the entry of
 * ngx_http_v2_close_stream() and check the connection's send window at
 * its return: above NGX_HTTP_V2_MAX_WINDOW, they kill the worker with
 * SIGKILL before it reads the window again. The master starts a new
 * worker; the worker's other connections are closed.
 *
 * gen_offsets.py reads NGINX2SHELL_PATH from this file and writes
 * nginx_h2c_send_window from the code of that binary's
 * ngx_http_v2_init(). Keep the define on one line.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#ifndef __TARGET_ARCH_x86
#define __TARGET_ARCH_x86
#endif
#include <bpf/bpf_tracing.h>

#define NGINX2SHELL_PATH	"/usr/sbin/nginx"

char _license[] SEC("license") = "Dual BSD/GPL";

#define SIGKILL			9
/* src/http/v2/ngx_http_v2.h */
#define NGX_HTTP_V2_MAX_WINDOW	((1ULL << 31) - 1)
/*
 * offsetof(ngx_http_v2_stream_t, connection): the struct starts with
 * request and connection in every release since 1.9.5; gen_offsets.py
 * checks that ngx_http_v2_close_stream() loads it from there.
 */
#define STREAM_CONNECTION	8

/*
 * offsetof(ngx_http_v2_connection_t, send_window). It moves between
 * releases and with distribution patches (0x50 in 1.18 to 1.31.6, 0x48
 * in 1.31.7 and 1.31.8, 0x60 in Ubuntu 24.04's 1.24.0), so
 * gen_offsets.py reads it from the binary at bpm build. 0 means the
 * package was built without the generator, and the guard does nothing.
 */
const volatile __u32 nginx_h2c_send_window = 0;

/*
 * The connection of the stream each nginx thread is closing, from the
 * entry of ngx_http_v2_close_stream() to its return. A worker runs
 * HTTP/2 on one thread, so this holds one entry per worker in the
 * call; LRU, so that an entry a killed worker leaves behind goes.
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 1024);
	__type(key, __u64);
	__type(value, __u64);
} closing SEC(".maps");

/* Workers killed with a send window above NGX_HTTP_V2_MAX_WINDOW. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} hits SEC(".maps");

/* Every stream close, the denominator of hits. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} activity SEC(".maps");

/* Closes not checked: no offset, or memory the program could not read. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} unchecked SEC(".maps");

static __always_inline void count_event(void *map)
{
	__u32 zero = 0;
	__u64 *count = bpf_map_lookup_elem(map, &zero);

	/* With preemptible RCU another run can interleave on this CPU. */
	if (count)
		__sync_fetch_and_add(count, 1);
}

SEC("uprobe/" NGINX2SHELL_PATH ":ngx_http_v2_close_stream")
int BPF_UPROBE(nginx2shell_close, const void *stream)
{
	__u64 id = bpf_get_current_pid_tgid();
	__u64 h2c = 0;

	if (!nginx_h2c_send_window)
		return 0;
	if (bpf_probe_read_user(&h2c, sizeof(h2c), stream + STREAM_CONNECTION) ||
	    !h2c)
		return 0;
	bpf_map_update_elem(&closing, &id, &h2c, BPF_ANY);
	return 0;
}

SEC("uretprobe/" NGINX2SHELL_PATH ":ngx_http_v2_close_stream")
int BPF_URETPROBE(nginx2shell_closed)
{
	__u64 id = bpf_get_current_pid_tgid();
	__u64 *entry, h2c, window;

	count_event(&activity);
	entry = bpf_map_lookup_elem(&closing, &id);
	if (!entry) {
		count_event(&unchecked);
		return 0;
	}
	h2c = *entry;
	bpf_map_delete_elem(&closing, &id);
	/* The connection outlives the call: its handler is only posted. */
	if (bpf_probe_read_user(&window, sizeof(window),
				(const void *)(h2c + nginx_h2c_send_window))) {
		count_event(&unchecked);
		return 0;
	}
	if (window <= NGX_HTTP_V2_MAX_WINDOW)
		return 0;
	count_event(&hits);
	bpf_send_signal(SIGKILL);
	return 0;
}

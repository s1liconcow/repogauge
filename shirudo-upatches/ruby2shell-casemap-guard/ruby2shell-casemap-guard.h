/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/*
 * Ruby2Shell, CVE-2026-132825 (GHSA-34m4-m9fh-gqp5): the case_map()
 * functions of the ISO-8859-x and Windows-125x encodings map the sharp s
 * (0xDF) to two bytes under ONIGENC_CASE_UPCASE or ONIGENC_CASE_FOLD,
 * but their loop ran while one byte of room was left, so the second
 * byte could land one past the output buffer.
 *
 * The fix (ruby/ruby d582f8f, in Ruby 3.3.14, 3.4.13 and 4.0.10) stops
 * the loop while one byte of room is left, for every byte:
 *
 *	-  while (*pp < end && to < to_end) {
 *	+  while (*pp < end && to_end - to > 1) {
 *
 * and rb_str_casemap() allocates another buffer for the rest. The same
 * releases carry GHSA-87fm-7w84-j2px (ruby/ruby bdff3c5d66dd), which
 * keeps every buffer case_map() fills within INT_MAX.
 *
 * A source file defines RUBY2SHELL_ENC_DIR, the enc/ directory of one
 * Ruby installation, and one program per encoding with
 * RUBY2SHELL_GUARD(). Each runs at the entry of that encoding's
 * case_map() and kills the process with SIGKILL when the call would
 * overrun its buffer under the old loop, or when the buffer exceeds
 * INT_MAX, before the function body runs. Every other call goes on as
 * it would have: the program replays the old loop's arithmetic over the
 * source bytes (ruby2shell-casemap-step.h) and writes nothing.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#ifndef __TARGET_ARCH_x86
#define __TARGET_ARCH_x86
#endif
#include <bpf/bpf_tracing.h>

#ifndef RUBY2SHELL_ENC_DIR
#error "define RUBY2SHELL_ENC_DIR, the absolute path of a Ruby enc/ directory"
#endif

char _license[] SEC("license") = "Dual BSD/GPL";

#define SIGKILL		9

#include "ruby2shell-casemap-step.h"

/* Processes killed before a sharp s overran a case_map() buffer. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} hits SEC(".maps");

/* Processes killed for a buffer beyond INT_MAX, GHSA-87fm-7w84-j2px. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} overflow SEC(".maps");

/* Processes killed for a source longer than CASEMAP_MAX_SCAN. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} unscanned SEC(".maps");

/* Calls let through because their arguments could not be read. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} unchecked SEC(".maps");

/* Every case_map() call of the guarded encodings. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} activity SEC(".maps");

static __always_inline void count_event(void *map)
{
	__u32 zero = 0;
	__u64 *count = bpf_map_lookup_elem(map, &zero);

	/* A sleepable run can be preempted by another on this CPU. */
	if (count)
		__sync_fetch_and_add(count, 1);
}

static __always_inline int kill_current(void *map)
{
	count_event(map);
	bpf_send_signal(SIGKILL);
	return 0;
}

/* One bpf_loop() step: copy the next chunk of the source and replay it. */
static long casemap_chunk(__u64 index, void *ctx)
{
	struct casemap_scan *sc = ctx;
	__u8 buf[CASEMAP_CHUNK];
	__u32 n = casemap_chunk_len(sc);

	barrier_var(n);
	if (n > CASEMAP_CHUNK)
		n = CASEMAP_CHUNK;
	if (bpf_copy_from_user(buf, n, sc->src + sc->done)) {
		sc->verdict = CASEMAP_UNREADABLE;
		return 1;
	}
	return casemap_replay(sc, buf, n);
}

static __always_inline int ruby2shell_check(const __u32 *flagp,
					    const __u8 *const *pp,
					    const __u8 *end, const __u8 *to,
					    const __u8 *to_end)
{
	struct casemap_scan sc = {};
	const __u8 *src;
	__u32 flags;

	count_event(&activity);
	if (to_end <= to)
		return 0;
	sc.room = to_end - to;
	/* The fixed rb_str_casemap() never passes a larger buffer. */
	if (sc.room > CASEMAP_INT_MAX)
		return kill_current(&overflow);
	if (bpf_copy_from_user(&src, sizeof(src), pp) ||
	    bpf_copy_from_user(&flags, sizeof(flags), flagp)) {
		count_event(&unchecked);
		return 0;
	}
	if (end <= src)
		return 0;
	switch (casemap_plan(&sc, flags, end - src)) {
	case CASEMAP_ALLOW:
		return 0;
	case CASEMAP_TOO_LONG:
		return kill_current(&unscanned);
	}
	sc.src = src;
	bpf_loop((sc.len + CASEMAP_CHUNK - 1) / CASEMAP_CHUNK, casemap_chunk,
		 &sc, 0);
	switch (sc.verdict) {
	case CASEMAP_OVERRUN:
		return kill_current(&hits);
	case CASEMAP_SAFE:
		return 0;
	default:
		/* unreadable, or bpf_loop() refused to run */
		count_event(&unchecked);
		return 0;
	}
}

/*
 * Sleepable, so that bpf_copy_from_user() faults in a source page that
 * is not resident rather than letting the call through unchecked.
 */
#define RUBY2SHELL_GUARD(enc)						\
SEC("uprobe.s/" RUBY2SHELL_ENC_DIR "/" #enc ".so:case_map")		\
int BPF_UPROBE(ruby2shell_##enc, const __u32 *flagp,			\
	       const __u8 *const *pp, const __u8 *end, const __u8 *to,	\
	       const __u8 *to_end)					\
{									\
	return ruby2shell_check(flagp, pp, end, to, to_end);		\
}

/* The fourteen encodings d582f8f fixes, each its own enc/<name>.so. */
#define RUBY2SHELL_GUARD_ALL()						\
	RUBY2SHELL_GUARD(iso_8859_1)					\
	RUBY2SHELL_GUARD(iso_8859_2)					\
	RUBY2SHELL_GUARD(iso_8859_3)					\
	RUBY2SHELL_GUARD(iso_8859_4)					\
	RUBY2SHELL_GUARD(iso_8859_9)					\
	RUBY2SHELL_GUARD(iso_8859_10)					\
	RUBY2SHELL_GUARD(iso_8859_13)					\
	RUBY2SHELL_GUARD(iso_8859_14)					\
	RUBY2SHELL_GUARD(iso_8859_15)					\
	RUBY2SHELL_GUARD(iso_8859_16)					\
	RUBY2SHELL_GUARD(windows_1250)					\
	RUBY2SHELL_GUARD(windows_1252)					\
	RUBY2SHELL_GUARD(windows_1254)					\
	RUBY2SHELL_GUARD(windows_1257)

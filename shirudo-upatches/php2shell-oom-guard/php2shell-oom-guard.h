/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/*
 * PHP2Shell, CVE-2026-109618 and CVE-2026-109619: a PHP process must not
 * go on after the Zend memory manager gives up on an allocation.
 *
 * Before PHP 8.5.12, 8.4.27, 8.3.36 and 8.2.35 (php-src 506e87fa7d,
 * "Avoid state corruption via bailout"), zend_mm_safe_error() reports
 * "Allowed memory size of ... exhausted" or "Out of memory" with
 * zend_error_noreturn(E_ERROR, ...) and then zend_bailout()s out of
 * whatever allocation ran, leaving the structures it was building half
 * initialized for the shutdown code and the next request. The size
 * checks of the allocator and of hash tables bailed out with "Possible
 * integer overflow in memory allocation" the same way. The fix records
 * these errors and unwinds with an UnwindExit instead.
 *
 * A source file includes this header once per PHP binary, after it
 * defines PHP2SHELL_PATH. The program runs at the entry of
 * zend_error_noreturn(), which every PHP 7.0 and later exports, and
 * kills the process with SIGKILL when the error is one of the formats
 * the fix stops bailing out with (the memory limit, out of memory, an
 * allocation size that would overflow), before PHP reports it or bails
 * out. A PHP-FPM or Apache master starts a new worker; the client of
 * the request sees the connection close instead of a fatal error page.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#ifndef __TARGET_ARCH_x86
#define __TARGET_ARCH_x86
#endif
#include <bpf/bpf_tracing.h>

#ifndef PHP2SHELL_PATH
#error "define PHP2SHELL_PATH, the absolute path of the PHP binary"
#endif

char _license[] SEC("license") = "Dual BSD/GPL";

#define E_ERROR		1
#define SIGKILL		9

/*
 * The formats of every bailout 506e87fa7d turns into a delayed error,
 * PHP 7.0 to 8.5: zend_mm_safe_error() in Zend/zend_alloc.c ("Allowed
 * memory size of", "Out of memory") and the size checks of
 * Zend/zend_alloc.c, zend_hash.c and zend_multiply.h ("Possible integer
 * overflow in memory allocation"). A first read of PREFIX_LEN bytes
 * tells them apart; the longer two are then read whole.
 */
#define OOM_PREFIX	"Out of memory"
#define LIMIT_PREFIX	"Allowed memory size of "
#define SIZE_PREFIX	"Possible integer overflow in memory allocation"
#define PREFIX_LEN	(sizeof(OOM_PREFIX) - 1)
#define LIMIT_LEN	(sizeof(LIMIT_PREFIX) - 1)
#define SIZE_LEN	(sizeof(SIZE_PREFIX) - 1)

/* Processes killed before a bailout out of an allocation. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} hits SEC(".maps");

/* Every zend_error_noreturn() call, the denominator of hits. */
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

/* Unrolled: each byte of the literal is an immediate of the program. */
static __always_inline bool prefix_eq(const char *buf, const char *lit,
				      __u32 len)
{
	__u32 i;

#pragma unroll
	for (i = 0; i < len; i++)
		if (buf[i] != lit[i])
			return false;
	return true;
}

/*
 * The format is a string literal of the PHP binary. Read no more than
 * the shortest prefix first, so a short format at the end of a mapping
 * is not read past, and a longer one only when its first bytes match.
 */
static __always_inline bool bailout_format(const char *format)
{
	char buf[SIZE_LEN];

	if (bpf_copy_from_user(buf, PREFIX_LEN, format))
		return false;
	if (prefix_eq(buf, OOM_PREFIX, PREFIX_LEN))
		return true;
	if (prefix_eq(buf, LIMIT_PREFIX, PREFIX_LEN)) {
		if (bpf_copy_from_user(buf, LIMIT_LEN, format))
			return false;
		return prefix_eq(buf, LIMIT_PREFIX, LIMIT_LEN);
	}
	if (prefix_eq(buf, SIZE_PREFIX, PREFIX_LEN)) {
		if (bpf_copy_from_user(buf, SIZE_LEN, format))
			return false;
		return prefix_eq(buf, SIZE_PREFIX, SIZE_LEN);
	}
	return false;
}

/*
 * Sleepable, so that bpf_copy_from_user() faults the page of the format
 * in rather than missing an evicted one. zend_error_noreturn() runs on
 * fatal errors only (on PHP 7.0 it is an alias of zend_error() and runs
 * on every error), so the cost is off every hot path.
 */
SEC("uprobe.s/" PHP2SHELL_PATH ":zend_error_noreturn")
int BPF_UPROBE(php2shell_oom_guard, int type, const char *format)
{
	count_event(&activity);
	if (type != E_ERROR || !format)
		return 0;
	if (!bailout_format(format))
		return 0;
	count_event(&hits);
	bpf_send_signal(SIGKILL);
	return 0;
}

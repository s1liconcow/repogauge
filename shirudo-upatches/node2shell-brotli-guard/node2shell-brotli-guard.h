/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/*
 * Node2Shell (HTTP2Shell), CVE-2026-111497: the Brotli decoder that
 * Node.js bundles took a block switch on a tree with a single block
 * type as a need for more input, not as a format error, and the
 * streaming decoder kept appending input bytes to its eight-byte
 * internal buffer.
 *
 * The fix (google/brotli 0ed4268, "Detect and report invalid stream
 * earlier", in Node.js 22.23.4, 24.22.1 and 26.12.1) makes
 * DecodeBlockTypeAndLength() return BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH
 * when s->num_block_types[tree] <= 1. No valid stream gets there: a
 * tree of one block type is never switched.
 *
 * DecodeBlockTypeAndLength() is always inlined. Its callers, the six
 * block switch functions (literal, command and distance, each in a fast
 * and a "safe" variant), are NOINLINE and named in the .symtab of the
 * official Node.js builds. A source file defines NODE2SHELL_PATH and one
 * program per function with NODE2SHELL_GUARD(). The program reads
 * s->num_block_types[tree] and kills the process with SIGKILL when it is
 * 0 or 1, before the function body runs.
 *
 * The offset of num_block_types differs between Brotli releases and
 * compilers. gen_offsets.py reads it from the code of the very functions
 * the uprobes name, in the binary bpm build resolves them in, and writes
 * it into brotli_num_block_types.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#ifndef __TARGET_ARCH_x86
#define __TARGET_ARCH_x86
#endif
#include <bpf/bpf_tracing.h>

#ifndef NODE2SHELL_PATH
#error "define NODE2SHELL_PATH, the absolute path of the node binary"
#endif

char _license[] SEC("license") = "Dual BSD/GPL";

#define SIGKILL		9

#define TREE_LITERAL	0
#define TREE_COMMAND	1
#define TREE_DISTANCE	2

/*
 * The offsets of s->num_block_types[0], [1] and [2] in
 * BrotliDecoderState, each a 64-bit brotli_reg_t; gen_offsets.py
 * writes them at bpm build. 0 means the package was built without the
 * generator, and the guard does nothing.
 */
const volatile __u32 brotli_num_block_types[3] = {};

/* Processes killed before a block switch on a tree of one block type. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} hits SEC(".maps");

/* Every block switch, the denominator of hits. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} activity SEC(".maps");

/* Runs of a build without offsets, or with a state it could not read. */
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

static __always_inline int node2shell_check(const void *s, __u32 tree)
{
	__u32 off = brotli_num_block_types[tree];
	__u64 num_block_types;

	count_event(&activity);
	if (!off || !s) {
		count_event(&unchecked);
		return 0;
	}
	/* The state is the decoder's own heap object, in use, resident. */
	if (bpf_probe_read_user(&num_block_types, sizeof(num_block_types),
				s + off)) {
		count_event(&unchecked);
		return 0;
	}
	if (num_block_types > 1)
		return 0;
	count_event(&hits);
	bpf_send_signal(SIGKILL);
	return 0;
}

/*
 * One program per block switch function. Each takes the decoder state
 * as its first argument; the GCC .constprop clones of v24 keep it.
 */
#define NODE2SHELL_GUARD(name, func, tree)				\
SEC("uprobe/" NODE2SHELL_PATH ":" func)					\
int BPF_UPROBE(name, const void *s)					\
{									\
	return node2shell_check(s, tree);				\
}

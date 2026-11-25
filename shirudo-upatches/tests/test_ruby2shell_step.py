# SPDX-License-Identifier: BSD-2-Clause
"""Check the decision of the Ruby2Shell case_map guard on the host.

Builds ruby2shell-casemap-step.h, the code the BPF program runs, into a
small C program together with a model of the case_map() loop of the
ISO-8859-x and Windows-125x encodings before ruby/ruby d582f8f. The model
follows that loop statement by statement and records each write past
to_end instead of making it. The guard must kill a call exactly when
the model overruns, for every flag combination String#upcase, #downcase,
#capitalize, #swapcase and their :fold forms pass, source lengths across
several chunks and rooms around the first-buffer size rb_str_casemap()
uses. Run with python3 -I or pytest; skips without a C compiler.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
STEP = HERE.parent / "ruby2shell-casemap-guard"

HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t __u8;
typedef uint32_t __u32;
typedef uint64_t __u64;
#ifndef __always_inline
#define __always_inline inline __attribute__((always_inline))
#endif

#include "ruby2shell-casemap-step.h"

/* case_map() of enc/iso_8859_1.c before d582f8f, writes checked, not made. */
static int old_loop_overruns(__u32 flags, const __u8 *p, const __u8 *end,
			     __u64 room)
{
	__u64 to = 0;

	while (p < end && to < room) {
		__u8 code = *p++;

		if (code == SHARP_S) {
			if (flags & ONIGENC_CASE_UPCASE) {
				if (to >= room)
					return 1;
				to++;		/* *to++ = 'S' */
			} else if (flags & ONIGENC_CASE_FOLD) {
				if (to >= room)
					return 1;
				to++;		/* *to++ = 's' */
			}
		}
		if (to >= room)
			return 1;
		to++;			/* *to++ = code */
		if (flags & ONIGENC_CASE_TITLECASE)
			flags ^= ONIGENC_CASE_UPCASE | ONIGENC_CASE_DOWNCASE |
				 ONIGENC_CASE_TITLECASE;
	}
	return 0;
}

/* ruby2shell_check() of ruby2shell-casemap-guard.h without the helpers. */
static int guard_kills(__u32 flags, const __u8 *src, __u64 srclen, __u64 room)
{
	struct casemap_scan sc;
	__u64 i, steps;

	memset(&sc, 0, sizeof(sc));
	if (!room)
		return 0;
	sc.room = room;
	if (sc.room > CASEMAP_INT_MAX)
		return 1;
	if (!srclen)
		return 0;
	switch (casemap_plan(&sc, flags, srclen)) {
	case CASEMAP_ALLOW:
		return 0;
	case CASEMAP_TOO_LONG:
		return 1;
	}
	sc.src = src;
	steps = (sc.len + CASEMAP_CHUNK - 1) / CASEMAP_CHUNK;
	for (i = 0; i < steps; i++) {
		__u8 buf[CASEMAP_CHUNK];
		__u32 n = casemap_chunk_len(&sc);

		memcpy(buf, sc.src + sc.done, n);
		if (casemap_replay(&sc, buf, n))
			break;
	}
	return sc.verdict == CASEMAP_OVERRUN;
}

static __u64 state = 0x2b992ddfa23249d6ULL;

static __u64 rnd(void)
{
	state ^= state << 13;
	state ^= state >> 7;
	state ^= state << 17;
	return state;
}

int main(void)
{
	/* upcase, downcase, capitalize, swapcase, their :fold forms, none */
	static const __u32 modes[] = {
		ONIGENC_CASE_UPCASE,
		ONIGENC_CASE_DOWNCASE,
		ONIGENC_CASE_UPCASE | ONIGENC_CASE_TITLECASE,
		ONIGENC_CASE_UPCASE | ONIGENC_CASE_DOWNCASE,
		ONIGENC_CASE_DOWNCASE | ONIGENC_CASE_FOLD,
		ONIGENC_CASE_FOLD,
		ONIGENC_CASE_TITLECASE,
		0,
	};
	/* bits case_map() carries but does not branch on for length */
	static const __u32 noise[] = { 0, 1U << 18, 1U << 20, 1U << 22 };
	static __u8 src[1200];
	unsigned long cases = 0, overruns = 0, replays = 0;
	int iter;

	for (iter = 0; iter < 400000; iter++) {
		__u32 flags = modes[rnd() % 8] | noise[rnd() % 4];
		__u64 len = rnd() % sizeof(src), room, i;
		unsigned sharp = rnd() % 100;
		int want, got;

		for (i = 0; i < len; i++) {
			unsigned r = rnd() % 100;

			src[i] = r < sharp ? SHARP_S :
				 r % 3 ? (__u8)('a' + rnd() % 26) : (__u8)(rnd() & 0xff);
		}
		switch (rnd() % 4) {
		case 0:
			room = len + 20;		/* first buffer */
			break;
		case 1:
			room = 2 * len + 20;		/* second buffer */
			break;
		case 2:
			room = len + rnd() % 40;	/* near the edge */
			break;
		default:
			room = rnd() % (2 * len + 64);
		}
		want = old_loop_overruns(flags, src, src + len, room);
		got = guard_kills(flags, src, len, room);
		if (want != got) {
			printf("MISMATCH flags=%#x len=%llu room=%llu old=%d guard=%d\n",
			       flags, (unsigned long long)len,
			       (unsigned long long)room, want, got);
			return 1;
		}
		cases++;
		overruns += want;
		if (room && len > room / 2)
			replays++;
	}
	/* Paths that need no source: an oversized buffer, a long source. */
	if (!guard_kills(ONIGENC_CASE_UPCASE, src, 1, CASEMAP_INT_MAX + 1) ||
	    guard_kills(ONIGENC_CASE_UPCASE, src, 1, CASEMAP_INT_MAX) ||
	    !guard_kills(ONIGENC_CASE_UPCASE, src, CASEMAP_MAX_SCAN + 1,
			 CASEMAP_MAX_SCAN + 21) ||
	    guard_kills(ONIGENC_CASE_DOWNCASE, src, CASEMAP_MAX_SCAN + 1,
			CASEMAP_MAX_SCAN + 21)) {
		printf("MISMATCH on the paths without a replay\n");
		return 1;
	}
	printf("cases=%lu overruns=%lu replays=%lu\n", cases, overruns, replays);
	return overruns < 1000 ? 2 : 0;
}
"""


class RubyCasemapStep(unittest.TestCase):
	def test_guard_matches_old_loop(self):
		cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
		if not cc:
			raise unittest.SkipTest("no C compiler")
		with tempfile.TemporaryDirectory() as tmp:
			c = Path(tmp) / "harness.c"
			exe = Path(tmp) / "harness"
			c.write_text(HARNESS)
			subprocess.run([cc, "-O2", "-Wall", "-Werror", "-I", str(STEP),
					str(c), "-o", str(exe)], check=True)
			run = subprocess.run([str(exe)], capture_output=True, text=True)
		self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
		print(run.stdout.strip())


if __name__ == "__main__":
	unittest.main()

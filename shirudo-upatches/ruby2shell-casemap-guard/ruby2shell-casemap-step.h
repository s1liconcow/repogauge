/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/*
 * The decision of the Ruby2Shell case_map guard, as arithmetic on the
 * output position of the case_map() loop of the ISO-8859-x and
 * Windows-125x encodings before ruby/ruby d582f8f. It reads no memory:
 * ruby2shell-casemap-guard.h copies the source bytes in and calls it,
 * and tests/test_ruby2shell_step.py builds it on the host and checks it
 * against a model of the old loop. The includer provides __u8, __u32,
 * __u64 and __always_inline.
 */

#ifndef RUBY2SHELL_CASEMAP_STEP_H
#define RUBY2SHELL_CASEMAP_STEP_H

/* include/ruby/onigmo.h, Ruby 3.3 to 4.0. */
#define ONIGENC_CASE_UPCASE	(1U << 13)
#define ONIGENC_CASE_DOWNCASE	(1U << 14)
#define ONIGENC_CASE_TITLECASE	(1U << 15)
#define ONIGENC_CASE_FOLD	(1U << 19)
/* enc/iso_8859.h */
#define SHARP_S			0xdf

#define CASEMAP_INT_MAX		0x7fffffffULL
/* Source bytes replayed per step of the caller's loop. */
#define CASEMAP_CHUNK		128
/* Source bytes replayed at most; a longer call is refused unchecked. */
#define CASEMAP_MAX_SCAN	(64ULL << 20)

enum casemap_plan {
	CASEMAP_ALLOW,		/* nothing can overrun: let the call go */
	CASEMAP_REPLAY,		/* replay sc->len source bytes */
	CASEMAP_TOO_LONG,	/* more than CASEMAP_MAX_SCAN bytes to replay */
};

enum casemap_verdict {
	CASEMAP_GOING,		/* the loop goes on */
	CASEMAP_SAFE,		/* the loop ends within the buffer */
	CASEMAP_OVERRUN,	/* the loop writes one byte past it */
	CASEMAP_UNREADABLE,	/* the caller could not copy the source */
};

struct casemap_scan {
	const __u8	*src;	/* *pp at entry, a user address */
	__u64		len;	/* source bytes the loop can consume */
	__u64		room;	/* to_end - to */
	__u64		pos;	/* output bytes written so far */
	__u64		done;	/* source bytes consumed so far */
	__u32		flags;	/* *flagP, as the loop updates it */
	__u32		verdict;
};

/*
 * Whether a call with srclen source bytes and sc->room bytes of room
 * needs a replay. Only a sharp s under UPCASE or FOLD writes two bytes,
 * and TITLECASE turns UPCASE on or off after the first byte. If every
 * source byte took two bytes and still fit, nothing can overrun: that is
 * every call but the one on the first buffer of rb_str_casemap(), whose
 * room is the source length plus 20.
 */
static __always_inline __u32 casemap_plan(struct casemap_scan *sc,
					  __u32 flags, __u64 srclen)
{
	if (!(flags & (ONIGENC_CASE_UPCASE | ONIGENC_CASE_FOLD |
		       ONIGENC_CASE_TITLECASE)))
		return CASEMAP_ALLOW;
	if (srclen <= sc->room / 2)
		return CASEMAP_ALLOW;
	/* Each step takes one source byte and writes one or two. */
	sc->len = srclen < sc->room ? srclen : sc->room;
	if (sc->len > CASEMAP_MAX_SCAN)
		return CASEMAP_TOO_LONG;
	sc->flags = flags;
	sc->pos = 0;
	sc->done = 0;
	sc->verdict = CASEMAP_GOING;
	return CASEMAP_REPLAY;
}

/*
 * One iteration of the old loop. It tests to < to_end before it takes a
 * byte; a sharp s under UPCASE or FOLD then writes two bytes, every
 * other byte one, and with one byte of room left the second lands past
 * to_end. The ctype tables decide what a byte maps to, never how many
 * bytes it writes, so the replay needs only the byte, the flags and the
 * room.
 */
static __always_inline __u32 casemap_step(struct casemap_scan *sc, __u8 c)
{
	if (sc->pos >= sc->room)
		return CASEMAP_SAFE;
	if (c == SHARP_S &&
	    (sc->flags & (ONIGENC_CASE_UPCASE | ONIGENC_CASE_FOLD))) {
		if (sc->room - sc->pos < 2)
			return CASEMAP_OVERRUN;
		sc->pos += 2;
	} else {
		sc->pos += 1;
	}
	/* capitalize: the first byte is titlecased, the rest downcased */
	if (sc->flags & ONIGENC_CASE_TITLECASE)
		sc->flags ^= ONIGENC_CASE_UPCASE | ONIGENC_CASE_DOWNCASE |
			     ONIGENC_CASE_TITLECASE;
	return CASEMAP_GOING;
}

/* How many source bytes the next chunk takes, at most CASEMAP_CHUNK. */
static __always_inline __u32 casemap_chunk_len(const struct casemap_scan *sc)
{
	__u64 left = sc->len - sc->done;

	return left < CASEMAP_CHUNK ? (__u32)left : CASEMAP_CHUNK;
}

/*
 * Replay the n source bytes of buf, the next chunk. Sets sc->verdict and
 * returns 1 once the loop's outcome is known, 0 to go on.
 */
static __always_inline int casemap_replay(struct casemap_scan *sc,
					  const __u8 *buf, __u32 n)
{
	__u32 i, verdict;

	for (i = 0; i < CASEMAP_CHUNK; i++) {
		if (i >= n)
			break;
		verdict = casemap_step(sc, buf[i]);
		if (verdict != CASEMAP_GOING) {
			sc->verdict = verdict;
			return 1;
		}
	}
	sc->done += n;
	if (sc->done >= sc->len) {
		sc->verdict = CASEMAP_SAFE;
		return 1;
	}
	return 0;
}

#endif

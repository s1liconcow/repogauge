// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
package upatch.java2shell;

import java.util.concurrent.atomic.AtomicLong;

/**
 * The check that java.util.Base64$Decoder.decode0 runs first once the
 * agent installed it. It rides in a jar on the bootstrap class path, so
 * that java.base can call it.
 *
 * The x86-64 AVX-512 VBMI decode stub of HotSpot before 8393212
 * (CVE-2026-114938) runs for non-MIME input longer than 31 bytes. After
 * its 64-byte blocks it decodes the tail, 1 to 63 bytes, itself, and for
 * a tail of 1 to 3 bytes whose last byte is '=' its padding arithmetic
 * goes wrong; the fix hands any tail shorter than 4 bytes to Java. The
 * stub sees the whole of decode0's range: a tail of 1 to 3 bytes after
 * the 64-byte blocks is a range of 65 bytes or more whose length is 1 to
 * 3 modulo 64.
 *
 * Such a range ending in '=' is never valid input for the basic and URL
 * decoders: a padded input must be a whole number of 4-byte units, and
 * its length is not. decode0 would throw IllegalArgumentException for
 * it; this throws the same exception before the stub runs, and changes
 * the outcome of no other input.
 */
public final class GuardCheck {
	private static final AtomicLong REFUSED = new AtomicLong();

	private GuardCheck() {}

	/** Called with decode0's src, sp, sl and the decoder's flags. */
	public static void check(byte[] src, int sp, int sl, boolean isURL, boolean isMIME) {
		if (isMIME)
			return;
		int length = sl - sp;
		if (length < 65)
			return;
		int tail = length & 63;
		if (tail < 1 || tail > 3 || src[sl - 1] != '=')
			return;
		if (REFUSED.getAndIncrement() == 0)
			System.err.println("java2shell-base64-guard: refused a padded Base64 input of "
					+ length + " bytes, CVE-2026-114938; later refusals are counted, not logged");
		throw new IllegalArgumentException(
				"Input byte array has wrong 4-byte ending unit (CVE-2026-114938 guard)");
	}

	/** How many inputs the check refused, for a diagnostic. */
	public static long refused() {
		return REFUSED.get();
	}
}

// SPDX-License-Identifier: BSD-2-Clause
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Random;

/**
 * Decodes a fixed, seeded set of inputs through every public entry point
 * of the basic, URL and MIME decoders and prints one line per call: the
 * case, then OK and a digest of the bytes, or EX and the exception
 * class. tests/test_java2shell_guard.py runs it with and without the
 * agent and compares. A line ends in " shape" for an input of the shape
 * the guard refuses.
 */
public final class Probe {
	private static final byte[] ALPHABET =
			"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_".getBytes();

	public static void main(String[] args) throws Exception {
		Random random = new Random(0x2b992ddfL);
		Base64.Decoder[] decoders = { Base64.getDecoder(), Base64.getUrlDecoder(), Base64.getMimeDecoder() };
		Base64.Encoder[] encoders = { Base64.getEncoder(), Base64.getUrlEncoder(), Base64.getMimeEncoder() };
		String[] names = { "basic", "url", "mime" };
		int n = 0;
		for (int d = 0; d < 3; d++) {
			for (int len = 0; len <= 700; len += 1 + random.nextInt(7)) {
				byte[] data = new byte[len];
				random.nextBytes(data);
				byte[] padded = encoders[d].encode(data);
				byte[] bare = encoders[d].withoutPadding().encode(data);
				probe(names[d] + " valid " + len, decoders[d], padded);
				probe(names[d] + " unpadded " + len, decoders[d], bare);
				probe(names[d] + " extra-pad " + len, decoders[d], concat(bare, "=".getBytes()));
				probe(names[d] + " extra-pad2 " + len, decoders[d], concat(bare, "==".getBytes()));
				if (padded.length > 3)
					probe(names[d] + " cut " + len, decoders[d],
							java.util.Arrays.copyOf(padded, padded.length - 1 - random.nextInt(3)));
				if (padded.length > 0) {
					byte[] bad = padded.clone();
					bad[random.nextInt(bad.length)] = (byte) "*\n=.\u0000".charAt(random.nextInt(5));
					probe(names[d] + " bad-char " + len, decoders[d], bad);
				}
				n++;
			}
			// The shape the guard refuses, around every residue that matters.
			for (int blocks = 1; blocks <= 6; blocks++) {
				for (int tail = 0; tail <= 5; tail++) {
					for (int pad = 0; pad <= 2; pad++) {
						int length = 64 * blocks + tail;
						byte[] in = new byte[length];
						for (int i = 0; i < length; i++)
							in[i] = ALPHABET[random.nextInt(d == 1 ? ALPHABET.length : 64)
									% (d == 1 ? ALPHABET.length : 64)];
						if (d == 1)
							for (int i = 0; i < length; i++)
								if (in[i] == '+' || in[i] == '/')
									in[i] = 'A';
						for (int p = 0; p < pad && p < length; p++)
							in[length - 1 - p] = '=';
						probe(names[d] + " block " + length + " pad " + pad, decoders[d], in);
						if (length > 2) {
							byte[] dirty = in.clone();
							dirty[length - 2 - pad] = '*';
							probe(names[d] + " block-dirty " + length + " pad " + pad, decoders[d], dirty);
						}
					}
				}
			}
		}
		System.out.println("cases " + n);
	}

	private static void probe(String name, Base64.Decoder decoder, byte[] in) throws Exception {
		boolean shape = decoder != Base64.getMimeDecoder() && in.length >= 65
				&& (in.length & 63) >= 1 && (in.length & 63) <= 3 && in[in.length - 1] == '=';
		String tag = shape ? " shape" : "";
		System.out.println(name + " bytes " + outcome(() -> decoder.decode(in)) + tag);
		System.out.println(name + " string "
				+ outcome(() -> decoder.decode(new String(in, StandardCharsets.ISO_8859_1))) + tag);
		System.out.println(name + " into " + outcome(() -> {
			byte[] dst = new byte[in.length];
			int w = decoder.decode(in, dst);
			return java.util.Arrays.copyOf(dst, w);
		}) + tag);
		// a heap buffer at an offset, and a direct one
		System.out.println(name + " heap " + outcome(() -> {
			byte[] wide = new byte[in.length + 11];
			System.arraycopy(in, 0, wide, 7, in.length);
			ByteBuffer b = ByteBuffer.wrap(wide, 7, in.length).slice();
			ByteBuffer out = decoder.decode(b);
			byte[] r = new byte[out.remaining()];
			out.get(r);
			return r;
		}) + tag);
		System.out.println(name + " direct " + outcome(() -> {
			ByteBuffer b = ByteBuffer.allocateDirect(in.length).put(in).flip();
			ByteBuffer out = decoder.decode(b);
			byte[] r = new byte[out.remaining()];
			out.get(r);
			return r;
		}) + tag);
	}

	private interface Call {
		byte[] run() throws Exception;
	}

	private static String outcome(Call call) throws Exception {
		try {
			byte[] r = call.run();
			return "OK " + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(r))
					.substring(0, 16);
		} catch (IllegalArgumentException e) {
			return "EX IllegalArgumentException";
		}
	}

	private static byte[] concat(byte[] a, byte[] b) {
		byte[] r = java.util.Arrays.copyOf(a, a.length + b.length);
		System.arraycopy(b, 0, r, a.length, b.length);
		return r;
	}
}

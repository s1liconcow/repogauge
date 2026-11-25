// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
package upatch.java2shell;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;

/**
 * Prepends a call of GuardCheck.check(src, sp, sl, isURL, isMIME) to
 * java.util.Base64$Decoder.decode0([BII[B)I in a class file, with no
 * library: the Class-File API is final only from JDK 24, and the JDKs
 * the CVE affects start at 17.
 *
 * The prologue is
 *
 *     aload_1; iload_2; iload_3
 *     aload_0; getfield isURL
 *     aload_0; getfield isMIME
 *     invokestatic GuardCheck.check([BIIZZ)V
 *     nop; nop
 *
 * 16 bytes, a multiple of 4, so the padding of a tableswitch or
 * lookupswitch, aligned to the start of the method, stays right. It
 * leaves the stack empty and the locals as they were, so the method's
 * implicit first frame holds at the old code's start; the code moves by
 * 16 bytes and every offset the Code attribute holds moves with it: the
 * exception table, the first StackMapTable frame (the others are
 * relative), the line numbers and the local variable ranges. A Code
 * attribute of a kind not listed here fails the patch rather than being
 * left with offsets it does not know.
 */
final class Decode0Patch {
	static final String CLASS = "java/util/Base64$Decoder";
	static final String METHOD = "decode0";
	static final String DESCRIPTOR = "([BII[B)I";
	private static final String CHECK_CLASS = "upatch/java2shell/GuardCheck";
	private static final String CHECK_NAME = "check";
	private static final String CHECK_DESCRIPTOR = "([BIIZZ)V";
	private static final int SHIFT = 16;

	private Decode0Patch() {}

	/** The patched class file, or an exception that says why not. */
	static byte[] apply(byte[] classFile) throws IOException {
		ByteBuffer in = ByteBuffer.wrap(classFile);
		if (in.getInt() != 0xCAFEBABE)
			throw new IOException("not a class file");
		int minor = u2(in), major = u2(in);
		if (major < 50)
			throw new IOException("class file version " + major + " has no StackMapTable");
		int cpCount = u2(in);
		String[] utf8 = new String[cpCount];
		int[] tags = new int[cpCount];
		int[] classNames = new int[cpCount];
		int cpStart = in.position();
		for (int i = 1; i < cpCount; i++) {
			int tag = in.get() & 0xff;
			tags[i] = tag;
			switch (tag) {
			case 1 -> {
				byte[] b = new byte[u2(in)];
				in.get(b);
				utf8[i] = modifiedUtf8(b);
			}
			case 7 -> classNames[i] = u2(in);
			case 8, 16, 19, 20 -> u2(in);
			case 15 -> { in.get(); u2(in); }
			case 3, 4, 9, 10, 11, 12, 17, 18 -> in.getInt();
			case 5, 6 -> { in.getLong(); i++; }
			default -> throw new IOException("constant pool tag " + tag);
			}
		}
		int cpEnd = in.position();
		int access = u2(in), thisClass = u2(in);
		if (tags[thisClass] != 7 || !CLASS.equals(utf8[classNames[thisClass]]))
			throw new IOException("not " + CLASS);

		// The new constant pool entries, appended after the old ones.
		ConstantPool cp = new ConstantPool(cpCount);
		int isUrlRef = cp.fieldref(thisClass, "isURL", "Z");
		int isMimeRef = cp.fieldref(thisClass, "isMIME", "Z");
		int checkRef = cp.methodref(cp.classInfo(CHECK_CLASS), CHECK_NAME, CHECK_DESCRIPTOR);
		byte[] prologue = {
			0x2b, 0x1c, 0x1d,				// aload_1 iload_2 iload_3
			0x2a, (byte) 0xb4, hi(isUrlRef), lo(isUrlRef),	// aload_0 getfield isURL
			0x2a, (byte) 0xb4, hi(isMimeRef), lo(isMimeRef),	// aload_0 getfield isMIME
			(byte) 0xb8, hi(checkRef), lo(checkRef),	// invokestatic check
			0x00, 0x00,					// nop nop
		};
		if (prologue.length != SHIFT)
			throw new AssertionError("prologue is " + prologue.length + " bytes");

		ByteArrayOutputStream bytes = new ByteArrayOutputStream(classFile.length + 256);
		DataOutputStream out = new DataOutputStream(bytes);
		out.writeInt(0xCAFEBABE);
		out.writeShort(minor);
		out.writeShort(major);
		out.writeShort(cp.count());
		out.write(classFile, cpStart, cpEnd - cpStart);
		cp.writeTo(out);
		out.writeShort(access);
		out.writeShort(thisClass);
		int superClass = u2(in);
		out.writeShort(superClass);
		int interfaces = u2(in);
		out.writeShort(interfaces);
		for (int i = 0; i < interfaces; i++)
			out.writeShort(u2(in));
		int fields = u2(in);
		out.writeShort(fields);
		for (int i = 0; i < fields; i++) {
			copy(in, out, 6);			// access, name, descriptor
			copyAttributes(in, out);
		}
		int methods = u2(in);
		out.writeShort(methods);
		int patched = 0;
		for (int i = 0; i < methods; i++) {
			int maccess = u2(in), name = u2(in), descriptor = u2(in);
			out.writeShort(maccess);
			out.writeShort(name);
			out.writeShort(descriptor);
			boolean target = METHOD.equals(utf8[name]) && DESCRIPTOR.equals(utf8[descriptor]);
			int attributes = u2(in);
			out.writeShort(attributes);
			for (int a = 0; a < attributes; a++) {
				int attrName = u2(in);
				int length = in.getInt();
				byte[] body = new byte[length];
				in.get(body);
				out.writeShort(attrName);
				if (target && "Code".equals(utf8[attrName])) {
					byte[] code = patchCode(body, prologue, utf8);
					out.writeInt(code.length);
					out.write(code);
					patched++;
				} else {
					out.writeInt(length);
					out.write(body);
				}
			}
		}
		if (patched != 1)
			throw new IOException(METHOD + DESCRIPTOR + " found " + patched + " times");
		out.write(classFile, in.position(), classFile.length - in.position());	// class attributes
		out.flush();
		return bytes.toByteArray();
	}

	private static byte[] patchCode(byte[] body, byte[] prologue, String[] utf8) throws IOException {
		ByteBuffer in = ByteBuffer.wrap(body);
		ByteArrayOutputStream bytes = new ByteArrayOutputStream(body.length + 64);
		DataOutputStream out = new DataOutputStream(bytes);
		int maxStack = u2(in), maxLocals = u2(in);
		if (maxLocals < 4)
			throw new IOException("decode0 has " + maxLocals + " locals");
		out.writeShort(Math.max(maxStack, 5));	// src, sp, sl, isURL, isMIME
		out.writeShort(maxLocals);
		int codeLength = in.getInt();
		if (codeLength + SHIFT > 65535)
			throw new IOException("decode0 too long to patch");
		out.writeInt(codeLength + SHIFT);
		out.write(prologue);
		byte[] code = new byte[codeLength];
		in.get(code);
		out.write(code);
		int exceptions = u2(in);
		out.writeShort(exceptions);
		for (int i = 0; i < exceptions; i++) {
			out.writeShort(u2(in) + SHIFT);	// start_pc
			out.writeShort(u2(in) + SHIFT);	// end_pc
			out.writeShort(u2(in) + SHIFT);	// handler_pc
			out.writeShort(u2(in));		// catch_type
		}
		int attributes = u2(in);
		out.writeShort(attributes);
		for (int a = 0; a < attributes; a++) {
			int name = u2(in);
			byte[] attr = new byte[in.getInt()];
			in.get(attr);
			byte[] shifted = switch (String.valueOf(utf8[name])) {
			case "StackMapTable" -> shiftStackMap(attr);
			case "LineNumberTable" -> shiftLineNumbers(attr);
			case "LocalVariableTable", "LocalVariableTypeTable" -> shiftLocals(attr);
			default -> throw new IOException("decode0 has a " + utf8[name]
					+ " attribute this patch cannot move");
			};
			out.writeShort(name);
			out.writeInt(shifted.length);
			out.write(shifted);
		}
		out.flush();
		return bytes.toByteArray();
	}

	/** Only the first frame's offset_delta is absolute; widen it if it must grow. */
	private static byte[] shiftStackMap(byte[] attr) throws IOException {
		ByteBuffer in = ByteBuffer.wrap(attr);
		ByteArrayOutputStream bytes = new ByteArrayOutputStream(attr.length + 2);
		DataOutputStream out = new DataOutputStream(bytes);
		int entries = u2(in);
		out.writeShort(entries);
		if (entries > 0) {
			int type = in.get() & 0xff;
			if (type <= 63) {				// same_frame
				writeSameFrame(out, type + SHIFT);
			} else if (type <= 127) {			// same_locals_1_stack_item
				int delta = type - 64 + SHIFT;
				if (delta <= 63) {
					out.writeByte(64 + delta);
				} else {
					out.writeByte(247);		// ..._extended
					out.writeShort(delta);
				}
			} else if (type >= 247) {			// every form with a u2 delta
				out.writeByte(type);
				out.writeShort(u2(in) + SHIFT);
			} else {
				throw new IOException("StackMapTable frame type " + type);
			}
		}
		out.write(attr, in.position(), attr.length - in.position());
		out.flush();
		return bytes.toByteArray();
	}

	private static void writeSameFrame(DataOutputStream out, int delta) throws IOException {
		if (delta <= 63) {
			out.writeByte(delta);
		} else {
			out.writeByte(251);				// same_frame_extended
			out.writeShort(delta);
		}
	}

	private static byte[] shiftLineNumbers(byte[] attr) {
		ByteBuffer b = ByteBuffer.wrap(attr.clone());
		int n = u2(b);
		for (int i = 0; i < n; i++) {
			int at = b.position();
			b.putShort(at, (short) (u2(b) + SHIFT));
			u2(b);						// line_number
		}
		return b.array();
	}

	/** A variable live from offset 0 (this, the parameters) stays live from 0. */
	private static byte[] shiftLocals(byte[] attr) {
		ByteBuffer b = ByteBuffer.wrap(attr.clone());
		int n = u2(b);
		for (int i = 0; i < n; i++) {
			int at = b.position();
			int start = u2(b), length = u2(b);
			if (start == 0)
				b.putShort(at + 2, (short) (length + SHIFT));
			else
				b.putShort(at, (short) (start + SHIFT));
			b.position(at + 10);
		}
		return b.array();
	}

	private static void copyAttributes(ByteBuffer in, DataOutputStream out) throws IOException {
		int attributes = u2(in);
		out.writeShort(attributes);
		for (int a = 0; a < attributes; a++) {
			out.writeShort(u2(in));
			int length = in.getInt();
			out.writeInt(length);
			copy(in, out, length);
		}
	}

	private static void copy(ByteBuffer in, DataOutputStream out, int n) throws IOException {
		byte[] b = new byte[n];
		in.get(b);
		out.write(b);
	}

	private static int u2(ByteBuffer b) {
		return b.getShort() & 0xffff;
	}

	private static byte hi(int v) {
		return (byte) (v >> 8);
	}

	private static byte lo(int v) {
		return (byte) v;
	}

	/** Modified UTF-8; the names compared here are ASCII, so a lossy read is fine. */
	private static String modifiedUtf8(byte[] b) {
		return new String(b, StandardCharsets.UTF_8);
	}

	/** Constant pool entries appended after an existing pool of count - 1 entries. */
	private static final class ConstantPool {
		private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
		private final DataOutputStream out = new DataOutputStream(bytes);
		private int next;

		ConstantPool(int count) {
			next = count;
		}

		int count() throws IOException {
			if (next > 65535)
				throw new IOException("constant pool full");
			return next;
		}

		void writeTo(DataOutputStream to) throws IOException {
			out.flush();
			to.write(bytes.toByteArray());
		}

		int utf8(String s) throws IOException {
			out.writeByte(1);
			out.writeUTF(s);
			return next++;
		}

		int classInfo(String internalName) throws IOException {
			int name = utf8(internalName);
			out.writeByte(7);
			out.writeShort(name);
			return next++;
		}

		int nameAndType(String name, String descriptor) throws IOException {
			int n = utf8(name), d = utf8(descriptor);
			out.writeByte(12);
			out.writeShort(n);
			out.writeShort(d);
			return next++;
		}

		int fieldref(int owner, String name, String descriptor) throws IOException {
			int nat = nameAndType(name, descriptor);
			out.writeByte(9);
			out.writeShort(owner);
			out.writeShort(nat);
			return next++;
		}

		int methodref(int owner, String name, String descriptor) throws IOException {
			int nat = nameAndType(name, descriptor);
			out.writeByte(10);
			out.writeShort(owner);
			out.writeShort(nat);
			return next++;
		}
	}
}

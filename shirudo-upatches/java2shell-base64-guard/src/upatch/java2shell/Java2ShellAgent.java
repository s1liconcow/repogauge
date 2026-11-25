// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
package upatch.java2shell;

import com.sun.tools.attach.VirtualMachine;
import java.io.File;
import java.io.InputStream;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.ProtectionDomain;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import java.util.jar.JarFile;

/**
 * Installs GuardCheck at the start of java.util.Base64$Decoder.decode0,
 * against CVE-2026-114938, in a JVM of release 17 or later. Agent, the
 * entry of -javaagent and of a dynamic attach, calls install(); main()
 * is the attach launcher: java -jar java2shell-base64-guard.jar <pid>.
 */
public final class Java2ShellAgent {
	private static final String GUARD_JAR = "/upatch/java2shell/guard-check.jar";
	private static boolean installed;

	private Java2ShellAgent() {}

	/** Dynamic-attach launcher: java -jar java2shell-base64-guard.jar <pid>. */
	public static void main(String[] args) throws Exception {
		if (args.length != 1)
			throw new IllegalArgumentException("usage: java -jar java2shell-base64-guard.jar <pid>");
		VirtualMachine vm = VirtualMachine.attach(args[0]);
		try {
			vm.loadAgent(new File(Java2ShellAgent.class.getProtectionDomain()
					.getCodeSource().getLocation().toURI()).getAbsolutePath());
		} finally {
			vm.detach();
		}
	}

	/** Called by Agent, which catches what this throws. */
	public static synchronized void install(Instrumentation inst) throws Exception {
		if (installed)
			return;
		Class<?> decoder = Class.forName("java.util.Base64$Decoder", false, null);
		Method decode0 = decoder.getDeclaredMethod("decode0", byte[].class, int.class,
				int.class, byte[].class);
		if (decode0.getReturnType() != int.class || !Modifier.isPrivate(decode0.getModifiers())
				|| decoder.getDeclaredField("isURL").getType() != boolean.class
				|| decoder.getDeclaredField("isMIME").getType() != boolean.class
				|| !inst.isRetransformClassesSupported() || !inst.isModifiableClass(decoder))
			throw new IllegalStateException("java.util.Base64$Decoder does not match this guard");

		// GuardCheck must be visible to the bootstrap loader, and java.base
		// must read its module, for the patched decode0 to link.
		appendGuardCheck(inst);
		Class<?> check = Class.forName("upatch.java2shell.GuardCheck", true, null);
		inst.redefineModule(Object.class.getModule(), Set.of(check.getModule()),
				Map.of(), Map.of(), Set.of(), Map.of());

		AtomicReference<Throwable> failure = new AtomicReference<>();
		AtomicReference<Boolean> done = new AtomicReference<>(false);
		ClassFileTransformer transformer = new ClassFileTransformer() {
			@Override
			public byte[] transform(Module module, ClassLoader loader, String name,
					Class<?> redefining, ProtectionDomain domain, byte[] bytes) {
				if (!Decode0Patch.CLASS.equals(name))
					return null;
				try {
					byte[] patched = Decode0Patch.apply(bytes);
					done.set(true);
					return patched;
				} catch (Throwable t) {
					failure.set(t);
					return null;
				}
			}
		};
		inst.addTransformer(transformer, true);
		try {
			inst.retransformClasses(decoder);
		} finally {
			inst.removeTransformer(transformer);
		}
		if (failure.get() != null)
			throw new IllegalStateException("cannot patch decode0", failure.get());
		if (!done.get())
			throw new IllegalStateException("decode0 was not patched");
		installed = true;
		System.err.println("java2shell-base64-guard: installed in java.util.Base64$Decoder.decode0");
	}

	private static void appendGuardCheck(Instrumentation inst) throws Exception {
		Path jar = Files.createTempFile("java2shell-guard-check", ".jar");
		jar.toFile().deleteOnExit();
		try (InputStream in = Java2ShellAgent.class.getResourceAsStream(GUARD_JAR)) {
			if (in == null)
				throw new IllegalStateException("guard-check.jar missing from the agent jar");
			Files.copy(in, jar, StandardCopyOption.REPLACE_EXISTING);
		}
		inst.appendToBootstrapClassLoaderSearch(new JarFile(jar.toFile()));
	}
}

// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
package upatch.java2shell;

import java.lang.instrument.Instrumentation;

/**
 * The agent's entry, built for Java 8 so that every JVM can load it.
 *
 * shirudo applies a Java upatch through JAVA_TOOL_OPTIONS to every JVM a
 * user manager starts, and a JVM whose -javaagent fails, by a premain
 * that throws or by an agent class it cannot load, does not start. So
 * this class never throws: in a JVM older than release 17, which the
 * CVE does not affect and whose class files cannot hold the installer,
 * it does nothing, and anywhere else it loads Java2ShellAgent by
 * reflection and reports a failure to install on standard error, and
 * the JVM goes on without the guard.
 */
public final class Agent {
	private Agent() {}

	public static void premain(String args, Instrumentation inst) {
		start(inst);
	}

	public static void agentmain(String args, Instrumentation inst) {
		start(inst);
	}

	private static void start(Instrumentation inst) {
		try {
			int release = release();
			if (release < 17) {
				System.err.println("java2shell-base64-guard: Java " + release
						+ " is not affected by CVE-2026-114938; not installed");
				return;
			}
			Class.forName("upatch.java2shell.Java2ShellAgent")
					.getMethod("install", Instrumentation.class).invoke(null, inst);
		} catch (Throwable t) {
			Throwable cause = t instanceof java.lang.reflect.InvocationTargetException
					&& t.getCause() != null ? t.getCause() : t;
			System.err.println("java2shell-base64-guard: not installed: " + cause);
		}
	}

	/** The feature release: "1.8" is 8, "17" is 17. */
	static int release() {
		String v = System.getProperty("java.specification.version", "0");
		if (v.startsWith("1."))
			v = v.substring(2);
		int dot = v.indexOf('.');
		try {
			return Integer.parseInt(dot < 0 ? v : v.substring(0, dot));
		} catch (NumberFormatException e) {
			return 0;
		}
	}
}

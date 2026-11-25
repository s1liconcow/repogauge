# SPDX-License-Identifier: BSD-2-Clause
"""Check the Java2Shell guard in a real JVM.

Builds java2shell-base64-guard.jar in a copy of the package, then runs
tests/java2shell/Probe.java twice in each JDK under test, without and
with the agent, both with the Base64 intrinsic disabled
(-XX:-UseBASE64Intrinsics), so that the stub the CVE is in never runs.
The guarded run also verifies java.base's classes, the patched one among
them. The agent must install, and the two runs must decode every input alike:
the only difference allowed is on inputs of the shape the guard refuses,
which must fail with IllegalArgumentException in both runs.

It also starts a JVM with the agent on its command line in every JVM
under test, Java 8 and 11 included: shirudo puts a Java upatch in the
JAVA_TOOL_OPTIONS of every JVM, and none may fail to start for it.

JVMs: the java on PATH, or the JDK and JRE homes JAVA2SHELL_JDKS lists,
separated by ':'. Run with python3 -I or pytest; skips without a JDK of
release 17 or newer.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "java2shell-base64-guard"
PROBE = HERE / "java2shell" / "Probe.java"
# No intrinsic, so the stub never runs; and the patched java.base class
# goes through the bytecode verifier, which boot classes otherwise skip.
OPTS = ["-XX:+UnlockDiagnosticVMOptions", "-XX:-UseBASE64Intrinsics",
	"-XX:+BytecodeVerificationLocal"]


def clean_env():
	env = dict(os.environ)
	# options a host injects into every JVM would change what is tested
	for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS"):
		env.pop(name, None)
	return env


def jdks():
	homes = os.environ.get("JAVA2SHELL_JDKS")
	if homes:
		bins = []
		for h in filter(None, homes.split(":")):
			# a Java 8 JRE keeps its java under jre/bin
			for b in (Path(h) / "bin", Path(h) / "jre" / "bin"):
				if (b / "java").exists():
					bins.append(b)
					break
		return bins
	java = shutil.which("java")
	return [Path(java).resolve().parent] if java else []


def release(bin_dir):
	"""The feature release: 1.8 is 8."""
	run = subprocess.run([str(bin_dir / "java"), "-XshowSettings:properties", "-version"],
			     capture_output=True, text=True, env=clean_env())
	m = re.search(r"java\.specification\.version = (?:1\.)?(\d+)", run.stderr)
	return int(m.group(1)) if m else 0


def builder():
	"""The bin of the first JDK of release 17 or newer with a javac, and its env."""
	for b in jdks():
		if (b / "javac").exists() and release(b) >= 17:
			env = clean_env()
			env["PATH"] = f"{b}:{env.get('PATH', '')}"
			return b, env
	raise unittest.SkipTest("no JDK of release 17 or newer")


def build_jar(tmp, env):
	pkg = Path(tmp) / "pkg"
	shutil.copytree(PKG, pkg, ignore=shutil.ignore_patterns("target", "*.jar"))
	subprocess.run([str(pkg / "build.sh"), "--jar-only"], check=True, env=env,
		       capture_output=True)
	return pkg / "java2shell-base64-guard.jar"


class Java2ShellGuard(unittest.TestCase):
	def test_every_jvm_starts(self):
		"""The agent never keeps a JVM from starting, whatever its release."""
		javac, env = builder()
		with tempfile.TemporaryDirectory() as tmp:
			jar = build_jar(tmp, env)
			src = Path(tmp) / "Hello.java"
			src.write_text("public final class Hello { public static void main(String[] a) "
				       "{ System.out.println(\"hello\"); } }\n")
			subprocess.run([str(javac / "javac"), "--release", "8", "-Xlint:-options",
					"-d", tmp, str(src)], check=True, env=env, capture_output=True)
			for b in jdks():
				r = release(b)
				with self.subTest(jvm=str(b), release=r):
					run = subprocess.run([str(b / "java"), f"-javaagent:{jar}", "-cp", tmp,
							      "Hello"], capture_output=True, text=True, env=env)
					self.assertEqual(run.returncode, 0, run.stderr)
					self.assertEqual(run.stdout.strip(), "hello")
					if r >= 17:
						self.assertIn("java2shell-base64-guard: installed", run.stderr)
					else:
						self.assertIn("is not affected by CVE-2026-114938", run.stderr)
					print(f"{b}: Java {r} starts, {run.stderr.strip().splitlines()[-1]}")

	def test_agent_changes_only_the_refused_shape(self):
		javac, env = builder()
		bins = [b for b in jdks() if release(b) >= 17]
		with tempfile.TemporaryDirectory() as tmp:
			jar = build_jar(tmp, env)
			classes = Path(tmp) / "probe"
			subprocess.run([str(javac / "javac"), "--release", "17", "-d", str(classes),
					str(PROBE)], check=True, env=env, capture_output=True)
			for b in bins:
				with self.subTest(jdk=str(b.parent)):
					self.compare(b, jar, classes, env)

	def test_dynamic_attach(self):
		"""java -jar <agent> <pid> patches a JVM that is already running."""
		javac, env = builder()
		target = (
			"import java.util.Base64;\n"
			"public final class Target {\n"
			"  public static void main(String[] a) throws Exception {\n"
			"    byte[] in = new byte[65];\n"
			"    java.util.Arrays.fill(in, (byte) 'A');\n"
			"    in[64] = '=';\n"
			"    System.out.println(\"ready\");\n"
			"    System.in.read();\n"
			"    try { Base64.getDecoder().decode(in); System.out.println(\"decoded\"); }\n"
			"    catch (IllegalArgumentException e) { System.out.println(e.getMessage()); }\n"
			"  }\n"
			"}\n")
		with tempfile.TemporaryDirectory() as tmp:
			jar = build_jar(tmp, env)
			src = Path(tmp) / "Target.java"
			src.write_text(target)
			subprocess.run([str(javac / "javac"), "-d", tmp, str(src)], check=True,
				       env=env, capture_output=True)
			# EnableDynamicAgentLoading is JDK 21's (JEP 451); older 17
			# builds do not know it and allow the attach anyway.
			proc = subprocess.Popen([str(javac / "java"), *OPTS[:2],
						 "-XX:+IgnoreUnrecognizedVMOptions",
						 "-XX:+EnableDynamicAgentLoading", "-cp", tmp, "Target"],
						stdin=subprocess.PIPE, stdout=subprocess.PIPE,
						stderr=subprocess.PIPE, text=True, env=env)
			try:
				self.assertEqual(proc.stdout.readline().strip(), "ready")
				attach = subprocess.run([str(javac / "java"), "-jar", str(jar), str(proc.pid)],
							capture_output=True, text=True, env=env, timeout=60)
				self.assertEqual(attach.returncode, 0, attach.stderr)
				out, err = proc.communicate("\n", timeout=60)
			finally:
				proc.kill()
		self.assertIn("installed", err)
		self.assertIn("CVE-2026-114938 guard", out)

	def compare(self, bin_dir, jar, classes, env):
		java = str(bin_dir / "java")
		plain = subprocess.run([java, *OPTS, "-cp", str(classes), "Probe"],
				       capture_output=True, text=True, env=env)
		guarded = subprocess.run([java, *OPTS, f"-javaagent:{jar}", "-cp", str(classes), "Probe"],
					 capture_output=True, text=True, env=env)
		self.assertEqual(plain.returncode, 0, plain.stderr)
		self.assertEqual(guarded.returncode, 0, guarded.stderr)
		self.assertIn("java2shell-base64-guard: installed", guarded.stderr)
		a, b = plain.stdout.splitlines(), guarded.stdout.splitlines()
		self.assertEqual(len(a), len(b))
		shapes = differ = 0
		for x, y in zip(a, b):
			if x.endswith(" shape"):
				shapes += 1
				self.assertIn(" EX IllegalArgumentException", x)
				self.assertIn(" EX IllegalArgumentException", y)
			elif x != y:
				differ += 1
				self.fail(f"outcome changed outside the refused shape:\n  {x}\n  {y}")
		self.assertGreater(shapes, 0)
		self.assertIn("refused a padded Base64 input", guarded.stderr)
		print(f"{bin_dir.parent}: {len(a)} decodes, {shapes} of the refused shape, "
		      f"{differ} other differences")


if __name__ == "__main__":
	unittest.main()

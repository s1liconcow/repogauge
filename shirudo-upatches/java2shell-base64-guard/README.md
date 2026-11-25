# java2shell-base64-guard

The `java2shell-base64-guard` package installs a Java upatch: an agent
that refuses, with `IllegalArgumentException`, the Base64 input the
HotSpot x86-64 decode stub mishandles in Java2Shell (also called
Java4Shell), CVE-2026-114938. It runs in every JVM of release 17 or
later. The bug is in OpenJDK 17.0.6 and later on x86-64 CPUs with
AVX-512 VBMI, fixed on 17 November 2026 (OpenJDK 8393212, "Base64
optimization improvements"); OpenJDK 8 and 11 are not affected.

## What the bug is and what the guard refuses

C2 replaces `java.util.Base64$Decoder.decodeBlock()` with a stub. On a
CPU with AVX-512 VBMI and for non-MIME input longer than 31 bytes, the
stub decodes 64-byte blocks, then decodes the 1 to 63 bytes left itself.
For a tail of 1 to 3 bytes whose last byte is `=`, its padding
arithmetic goes wrong and it writes past the destination array. The fix
hands any tail shorter than 4 bytes to the Java code. The stub sees the
whole range `decode0()` is called with, so that tail exists exactly when
the range is 65 bytes or more and its length is 1 to 3 modulo 64.

The agent puts a call of `GuardCheck.check()` at the start of
`Decoder.decode0()`, the caller, which C2 does not replace. The check
throws `IllegalArgumentException` for a non-MIME range of that length
whose last byte is `=`. Such a range is never valid input for the basic
and URL decoders: padded input is a whole number of 4-byte units, and
its length is not, so `decode0()` throws the same exception for it
anyway. Every other input decodes as before. The first refusal is
logged to standard error, without the input; later ones are counted.

`tests/test_java2shell_guard.py` checks exactly that, in OpenJDK 17, 21
and 25. It decodes 18,751 inputs (valid, unpadded, cut, padded wrong,
with bad bytes, and 1 to 6 blocks of 64 bytes plus 0 to 5, padded with
0 to 2 `=`, clean and with a bad byte) through every public entry point
of the three decoders, with and
without the agent. The only differences are the 890 inputs of the
refused shape, which throw `IllegalArgumentException` in both runs. The
runs disable the intrinsic, so the stub never runs, and verify the
patched class with `-XX:+BytecodeVerificationLocal`.

## Why not shirudo's java-base64

shirudo's own `upatches/java-base64` guards the same method. This
package differs in three ways:

- `java-base64` rewrites the class with the Class-File API, which is in
  the JDK from release 24 only, so it cannot run in a JVM of release 17
  or 21, the long-term releases most of the affected JVMs run. This
  agent patches `decode0()` with a class-file editor of its own
  (`Decode0Patch.java`), built for release 17.
- Built as its README says, with a JDK of release 24 or newer,
  `java-base64.jar` holds class files of that release, and a JVM of
  release 17 or 21 with it in `-javaagent` does not start: it fails with
  `UnsupportedClassVersionError` and "FATAL ERROR in native method:
  processing of -javaagent failed", exit status 134 (checked here on
  OpenJDK 17.0.21 and 21.0.13). Through the `JAVA_TOOL_OPTIONS` shirudo
  sets, that is every such JVM a user manager starts. This agent's entry
  class (`Agent.java`) is built for Java 8 and never throws: in a JVM
  older than 17 it says it is not needed and does nothing, and a failure
  to install is reported on standard error while the JVM goes on.
- `java-base64` lets the input through when a byte before the padding
  is outside the alphabet. The pre-fix stub adjusts the tail for the
  padding before it checks the tail's bytes, and the fix refuses every
  short tail whatever its bytes, so this agent refuses the shape
  without that condition. That refuses nothing more than input the
  decoder rejects anyway.

Load one of the two, not both: in a JDK 24 or later JVM both work, and
two checks do the work of one.

## How it reaches a JVM

- As a shirudo Java upatch, at JVM start, through the `-javaagent` of
  the `JAVA_TOOL_OPTIONS` that `/run/environment.d/shirudo.conf` sets
  (shirudo's `Documentation/upatches/write/java-agents.rst`). It reaches
  the JVMs a systemd user manager starts after the upatch is loaded.
- For a JVM shirudo does not start, as in a container, the same jar
  attaches to it while it runs: `java -jar java2shell-base64-guard.jar
  <pid>`. A JVM of release 21 or later warns about an agent loaded this
  way unless it was started with `-XX:+EnableDynamicAgentLoading`
  (JEP 451); the test attaches with that option set.

The agent checks that `Decoder.decode0([BII[B)I` and the `isURL` and
`isMIME` fields are what it expects, and does not install, with a
message, if not; the JVM then starts and decodes as before. In a JVM of
release 8 or 11, which the CVE does not affect, it does nothing. It is
a mitigation inside the application, not a boundary against root.

## Build and publish

Build in a shirudo checkout, with the directory under `upatches/`, with
a JDK of release 17 or later (`javac` and `jar` on `PATH`):

```sh
upatches/java2shell-base64-guard/build.sh 1.0.0 --keys-dir <keys>
shirudo bpm info java2shell-base64-guard-1.0.0.bee
shirudo bpm push java2shell-base64-guard-1.0.0.bee oci://<registry>/<namespace>/java2shell-base64-guard --keys-dir <keys>
shirudo bpm tree append java2shell-base64-guard-1.0.0.bee oci://<registry>/<tree> --keys-dir <keys>
```

`build.sh --jar-only` builds only the jar. The tree entry selects
x86-64 nodes: only x86-64 has the stub.

## Limits

- Only JVMs the agent reaches are guarded.
- `-XX:-UseBASE64Intrinsics` closes the bug too, at JVM start, at the
  cost of the intrinsic; this package is for the JVMs whose options you
  do not control.
- Validated here: the jar builds with `-Xlint:all -Werror`, the test
  above passes in OpenJDK 17.0.21, 21.0.13 and 25.0.5, at JVM start and
  by dynamic attach, a JVM of OpenJDK 8, 11, 17, 21 and 25 starts with
  the agent in `-javaagent`, and one whose install fails (a jar without
  `guard-check.jar`) starts and reports it. Not yet done: `bpm build`,
  signing and a load through shirudo's `environment.d` in its test VM,
  and a run on a CPU with AVX-512 VBMI and the intrinsic on.
- Upgrade the JDK.

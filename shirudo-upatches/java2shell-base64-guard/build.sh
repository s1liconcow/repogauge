#!/bin/sh
# SPDX-License-Identifier: BSD-2-Clause
#
# Build the agent jar, then the package into the current directory:
#   java2shell-base64-guard/build.sh [--jar-only] <version> [bpm build options...]
#
# Needs a JDK of release 17 or newer (javac and jar on PATH). Agent, the
# entry the manifest names, is built for Java 8, so that every JVM loads
# it and none fails to start for it; the rest is built for Java 17, the
# first release the CVE affects. GuardCheck rides alone in a
# nested guard-check.jar that the agent appends to the bootstrap class
# path, so that java.util.Base64 can call it; the whole agent is one
# signed Java upatch.

set -eu

jar_only=0
if [ "${1:-}" = --jar-only ]; then jar_only=1; shift; fi
if [ "$jar_only" -eq 0 ]; then
	[ $# -ge 1 ] || { echo "usage: $0 [--jar-only] <version> [bpm build options...]" >&2; exit 2; }
	version=$1
	shift
fi
pkg=$(cd "$(dirname "$0")" && pwd)
start=$PWD

cd "$pkg"
rm -rf target
mkdir -p target/classes target/stage/upatch/java2shell
javac --release 8 -Xlint:all,-options -Werror -d target/classes \
	src/upatch/java2shell/Agent.java
javac --release 17 -Xlint:all -Werror -d target/classes -cp target/classes \
	$(ls src/upatch/java2shell/*.java | grep -v '/Agent\.java$')
# GuardCheck alone, for the bootstrap loader; the agent's own classes
# stay off the bootstrap class path.
jar cf target/guard-check.jar -C target/classes upatch/java2shell/GuardCheck.class
for c in target/classes/upatch/java2shell/*.class; do
	case $(basename "$c") in GuardCheck.class) ;; *) cp "$c" target/stage/upatch/java2shell/ ;; esac
done
cp target/guard-check.jar target/stage/upatch/java2shell/guard-check.jar
cat > target/MANIFEST.MF <<'MF'
Manifest-Version: 1.0
Premain-Class: upatch.java2shell.Agent
Agent-Class: upatch.java2shell.Agent
Main-Class: upatch.java2shell.Java2ShellAgent
Can-Retransform-Classes: true
MF
jar cfm java2shell-base64-guard.jar target/MANIFEST.MF -C target/stage .

[ "$jar_only" -eq 0 ] || exit 0
cd "$start"
exec shirudo bpm build "$pkg" --version "$version" "$@"

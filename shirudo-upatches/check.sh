#!/bin/sh
# SPDX-License-Identifier: BSD-2-Clause
#
# Compile every upatch source of this tree the way shirudo build does
# (tools/shirudo-build/compile.c: clang -target bpf -mcpu=v4
# -fno-jump-tables -O2 -g), list the program sections of each object,
# and run the host tests of tests/.
#
#   SHIRUDO_SRC=<shirudo checkout> shirudo-upatches/check.sh
#
# The checkout gives vendor/libbpf, bpf/lib and a vmlinux.h
# (vendor/linux/vmlinux.h, or upatches/kernel-cve/vmlinux.h).
# CLANG defaults to clang; OUT names the directory for the objects and
# test logs, a new temporary one by default. This compiles; it does not
# sign or load: a load in shirudo's test VM is what runs the verifier.

set -eu

here=$(cd "$(dirname "$0")" && pwd)
src=${SHIRUDO_SRC:?set SHIRUDO_SRC to a shirudo checkout}
clang=${CLANG:-clang}
out=${OUT:-$(mktemp -d)}

vmlinux=
for v in "$src/vendor/linux/vmlinux.h" "$src/upatches/kernel-cve/vmlinux.h"; do
	if [ -f "$v" ]; then vmlinux=$v; break; fi
done
[ -n "$vmlinux" ] || { echo "no vmlinux.h in $src" >&2; exit 1; }

fail=0
for c in "$here"/*/*.bpf.c; do
	pkg=$(basename "$(dirname "$c")")
	obj="$out/$pkg-$(basename "$c" .bpf.c).bpf.o"
	# -Wall -Werror holds for this tree's code; the generated
	# vmlinux.h and libbpf are system headers and keep their warnings.
	if ! "$clang" -target bpf -mcpu=v4 -fno-jump-tables -O2 -g \
		-Wall -Werror \
		-isystem "$(dirname "$vmlinux")" -isystem "$src/vendor/libbpf" \
		-isystem "$src/bpf/lib" -I"$(dirname "$c")" \
		-c "$c" -o "$obj"; then
		echo "FAIL compile $pkg/$(basename "$c")"
		fail=1
		continue
	fi
	progs=$(llvm-readelf -S "$obj" |
		grep -oE ' (uprobe|uretprobe|lsm)[^ ]*' | tr -d '\n' || true)
	echo "ok   $pkg/$(basename "$c"): $progs"
done

for t in "$here"/tests/test_*.py; do
	[ -f "$t" ] || continue
	if python3 -I "$t" >"$out/$(basename "$t").log" 2>&1; then
		echo "ok   tests/$(basename "$t")"
	else
		echo "FAIL tests/$(basename "$t") (log: $out/$(basename "$t").log)"
		fail=1
	fi
done

exit $fail

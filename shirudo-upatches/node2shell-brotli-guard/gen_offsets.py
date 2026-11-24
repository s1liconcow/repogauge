#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Generate brotli_num_block_types of node2shell-brotli-guard.

bpm build runs this with --input <source>.bpf.c. It reads the binary
path (NODE2SHELL_PATH) and the guarded functions (NODE2SHELL_GUARD()
lines) from that source, finds each function in the binary's symbol
tables, and reads the offset of s->num_block_types[tree] from the loads
off the first argument (%rdi) at the start of each function. The three
trees must agree on one base X, with the literal tree at X, the command
tree at X + 8 and the distance tree at X + 16 (brotli_reg_t is 64 bits
on x86-64), or the build fails: a guard with a wrong offset would read
another field.
"""

import argparse
import mmap
from pathlib import Path
import re
import struct

TREES = {"TREE_LITERAL": 0, "TREE_COMMAND": 1, "TREE_DISTANCE": 2}
SCAN = 96          # bytes of each function to look for the load in
STRIDE = 8         # sizeof(brotli_reg_t) on x86-64
SANE = range(0x40, 0x1000)

PATH_RE = re.compile(r'^#define\s+NODE2SHELL_PATH\s+"(/[^"]+)"', re.M)
GUARD_RE = re.compile(r'^NODE2SHELL_GUARD\(\s*\w+\s*,\s*"([^"]+)"\s*,'
		      r'\s*(TREE_\w+)\s*\)', re.M)


class Elf:
	"""The function symbols of a little-endian x86-64 ELF file."""

	def __init__(self, path):
		self.path = path
		with open(path, "rb") as stream:
			self.data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
		d = self.data
		if d[:4] != b"\x7fELF" or d[4] != 2 or d[5] != 1:
			raise ValueError(f"{path}: not a 64-bit little-endian ELF file")
		if struct.unpack_from("<H", d, 18)[0] != 62:
			raise ValueError(f"{path}: not an x86-64 ELF file")
		shoff, = struct.unpack_from("<Q", d, 40)
		shentsize, shnum = struct.unpack_from("<HH", d, 58)
		self.sections = []
		for i in range(shnum):
			name, kind, _, addr, off, size, link, _, _, entsize = \
				struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize)
			self.sections.append((kind, addr, off, size, link, entsize))
		self.funcs = self._index()

	def _index(self):
		"""Every defined FUNC of .symtab and .dynsym: name -> {(offset, size)}."""
		funcs = {}
		d = self.data
		for kind, _, off, size, link, entsize in self.sections:
			if kind not in (2, 11) or not entsize:	# SHT_SYMTAB, SHT_DYNSYM
				continue
			stroff = self.sections[link][2]
			for st_name, info, _, shndx, value, fsize in \
					struct.iter_unpack("<IBBHQQ", d[off:off + size - size % entsize]):
				if info & 0xf != 2 or not shndx or shndx >= len(self.sections):
					continue	# STT_FUNC, defined
				start = stroff + st_name
				name = d[start:d.find(b"\0", start)]
				_, saddr, soff, _, _, _ = self.sections[shndx]
				funcs.setdefault(name, set()).add((soff + value - saddr, fsize))
		return funcs

	def code(self, name):
		found = self.funcs.get(name.encode(), set())
		if not found:
			raise ValueError(f"{self.path}: no function {name}")
		if len({off for off, _ in found}) > 1:
			raise ValueError(f"{self.path}: {name} names several functions")
		off, size = max(found, key=lambda f: f[1])
		return bytes(self.data[off:off + max(1, min(size or SCAN, SCAN))])


def rdi_loads(code):
	"""disp32 of each `mov disp32(%rdi), %r64` (REX.W 8B /r, mod 10, rm 111)."""
	disps = set()
	for i in range(len(code) - 6):
		rex, op, modrm = code[i], code[i + 1], code[i + 2]
		if rex & 0xf8 == 0x48 and not rex & 0x1 and op == 0x8b \
				and modrm & 0xc7 == 0x87:
			disps.add(struct.unpack_from("<i", code, i + 3)[0])
	return disps


def offsets(source, binary=None):
	text = Path(source).read_text()
	path = PATH_RE.search(text)
	guards = GUARD_RE.findall(text)
	if not path or not guards:
		raise ValueError(f"{source}: no NODE2SHELL_PATH or NODE2SHELL_GUARD()")
	elf = Elf(binary or path.group(1))
	loads = []
	for func, tree in guards:
		if tree not in TREES:
			raise ValueError(f"{source}: unknown tree {tree}")
		loads.append((func, TREES[tree], rdi_loads(elf.code(func))))
	bases = None
	for func, tree, disps in loads:
		mine = {d - STRIDE * tree for d in disps}
		bases = mine if bases is None else bases & mine
	bases = {b for b in bases if b in SANE and b % STRIDE == 0}
	if len(bases) != 1:
		detail = ", ".join(f"{f}: {sorted(hex(d) for d in s)}" for f, _, s in loads)
		raise ValueError(f"{elf.path}: no single num_block_types offset ({detail})")
	base = bases.pop()
	return [base + STRIDE * tree for tree in range(3)]


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, type=Path)
	parser.add_argument("--symbol", required=True)
	parser.add_argument("--output", required=True, type=Path)
	parser.add_argument("--binary", type=Path,
			    help="read this file instead of NODE2SHELL_PATH (tests)")
	args = parser.parse_args()
	try:
		if args.symbol != "brotli_num_block_types":
			raise ValueError(f"unsupported symbol: {args.symbol}")
		values = offsets(args.input, args.binary)
		args.output.write_bytes(struct.pack("<3I", *values))
	except (OSError, ValueError) as error:
		parser.error(str(error))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

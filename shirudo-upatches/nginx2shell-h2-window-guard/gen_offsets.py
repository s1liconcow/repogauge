#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Generate nginx_h2c_send_window of nginx2shell-h2-window-guard.

bpm build runs this with --input <source>.bpf.c. It reads the binary
path (NGINX2SHELL_PATH) from that source and the offset of
ngx_http_v2_connection_t.send_window from the binary's own
ngx_http_v2_init(), which every nginx since 1.9.5 exports and which sets

    h2c->send_window = NGX_HTTP_V2_DEFAULT_WINDOW;   /* 65535 */
    h2c->recv_window = NGX_HTTP_V2_MAX_WINDOW;       /* 0x7fffffff */
    h2c->init_window = NGX_HTTP_V2_DEFAULT_WINDOW;   /* 65535 */

on three consecutive size_t fields: three `movq $imm32, disp(%reg)` with
one base register at disp, disp + 8 and disp + 16. It also checks that
ngx_http_v2_close_stream() loads stream->connection from offset 8, the
STREAM_CONNECTION of the source. Anything else fails the build: a guard
with a wrong offset would read another field. --send-window takes the
offset as given instead, for a build whose compiler stored the fields
another way (README.md says how to find it).
"""

import argparse
import mmap
from pathlib import Path
import re
import struct

SCAN = 2048        # bytes of ngx_http_v2_init to look for the stores in
DEFAULT_WINDOW = 65535
MAX_WINDOW = 0x7fffffff
STREAM_CONNECTION = 8
SANE = range(0x10, 0x400)

PATH_RE = re.compile(r'^#define\s+NGINX2SHELL_PATH\s+"(/[^"]+)"', re.M)


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

	def code(self, name, scan):
		found = self.funcs.get(name.encode(), set())
		if not found:
			raise ValueError(f"{self.path}: no function {name}")
		if len({off for off, _ in found}) > 1:
			raise ValueError(f"{self.path}: {name} names several functions")
		off, size = max(found, key=lambda f: f[1])
		return bytes(self.data[off:off + max(1, min(size or scan, scan))])


def modrm_mem(code, i, rex):
	"""Decode the memory operand at code[i]: (base, disp, length) or None."""
	modrm = code[i]
	mod, rm = modrm >> 6, modrm & 7
	if mod == 3 or (mod == 0 and rm == 5):
		return None		# a register, or %rip-relative
	n = 1
	if rm == 4:			# SIB: only a base, no index
		sib = code[i + 1]
		# index 100 is none unless REX.X makes it %r12
		if (sib >> 3) & 7 != 4 or rex & 0x2 or (mod == 0 and sib & 7 == 5):
			return None
		rm = sib & 7
		n += 1
	base = rm | (rex & 1) << 3
	if mod == 1:
		disp = struct.unpack_from("<b", code, i + n)[0]
		n += 1
	elif mod == 2:
		disp = struct.unpack_from("<i", code, i + n)[0]
		n += 4
	else:
		disp = 0
	return base, disp, n


def imm_stores(code):
	"""(base, disp, imm) of each `movq $imm32, disp(%base)` (REX.W C7 /0)."""
	stores = []
	for i in range(len(code) - 8):
		rex, op = code[i], code[i + 1]
		if rex & 0xf8 != 0x48 or op != 0xc7 or (code[i + 2] >> 3) & 7:
			continue
		mem = modrm_mem(code, i + 2, rex)
		if not mem:
			continue
		base, disp, n = mem
		if i + 2 + n + 4 > len(code):
			continue
		imm = struct.unpack_from("<i", code, i + 2 + n)[0]
		stores.append((base, disp, imm))
	return stores


def rdi_loads(code):
	"""disp of each `mov disp(%rdi), %r64` (REX.W 8B /r, base %rdi)."""
	disps = set()
	for i in range(len(code) - 3):
		rex, op = code[i], code[i + 1]
		if rex & 0xf9 != 0x48 or op != 0x8b:	# REX.W, no REX.B
			continue
		mem = modrm_mem(code, i + 2, rex)
		if mem and mem[0] == 7:
			disps.add(mem[1])
	return disps


def send_window(elf):
	stores = set(imm_stores(elf.code("ngx_http_v2_init", SCAN)))
	found = {disp for base, disp, imm in stores
		 if imm == DEFAULT_WINDOW
		 and (base, disp + 8, MAX_WINDOW) in stores
		 and (base, disp + 16, DEFAULT_WINDOW) in stores
		 and disp in SANE and disp % 8 == 0}
	if len(found) != 1:
		raise ValueError(f"{elf.path}: ngx_http_v2_init() does not show one "
				 f"send_window offset ({sorted(hex(d) for d in found)}); "
				 "pass --send-window")
	return found.pop()


def check_stream_connection(elf):
	loads = rdi_loads(elf.code("ngx_http_v2_close_stream", 96))
	if STREAM_CONNECTION not in loads:
		raise ValueError(f"{elf.path}: ngx_http_v2_close_stream() does not load "
				 f"stream->connection from offset {STREAM_CONNECTION}")


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, type=Path)
	parser.add_argument("--symbol", required=True)
	parser.add_argument("--output", required=True, type=Path)
	parser.add_argument("--binary", type=Path,
			    help="read this file instead of NGINX2SHELL_PATH (tests)")
	parser.add_argument("--send-window", type=lambda v: int(v, 0),
			    help="use this offset instead of reading ngx_http_v2_init()")
	args = parser.parse_args()
	try:
		if args.symbol != "nginx_h2c_send_window":
			raise ValueError(f"unsupported symbol: {args.symbol}")
		path = PATH_RE.search(args.input.read_text())
		if not path:
			raise ValueError(f"{args.input}: no NGINX2SHELL_PATH")
		elf = Elf(args.binary or path.group(1))
		check_stream_connection(elf)
		if args.send_window is not None:
			if args.send_window not in SANE or args.send_window % 8:
				raise ValueError(f"--send-window {args.send_window:#x} is no "
						 "size_t field offset")
			offset = args.send_window
		else:
			offset = send_window(elf)
		args.output.write_bytes(struct.pack("<I", offset))
	except (OSError, ValueError) as error:
		parser.error(str(error))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

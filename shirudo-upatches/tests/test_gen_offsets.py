# SPDX-License-Identifier: BSD-2-Clause
"""Check the offset generators of the Node.js and nginx guards.

Each generator finds functions in an ELF file and reads a struct offset
from their machine code. The tests build small ELF files with a C
compiler whose functions carry the code shapes the generators look for,
written as inline assembly so that the compiler cannot reorder or merge
them, and check the offsets the generators write, and that they refuse
a binary whose code does not agree.

Real binaries, when given, are checked as well:
  NODE2SHELL_NODE=<node> [NODE2SHELL_SOURCE=<.bpf.c>]  num_block_types
  NGINX2SHELL_NGINX=<nginx>                            send_window
Run with python3 -I or pytest; skips without a C compiler.
"""

import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
NODE = HERE.parent / "node2shell-brotli-guard"
NGINX = HERE.parent / "nginx2shell-h2-window-guard"


def cc():
	return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def build(tmp, name, body):
	if sys.platform != "linux" or os.uname().machine != "x86_64":
		raise unittest.SkipTest("needs an x86-64 Linux host")
	compiler = cc()
	if not compiler:
		raise unittest.SkipTest("no C compiler")
	src = Path(tmp) / f"{name}.c"
	out = Path(tmp) / name
	src.write_text(body)
	subprocess.run([compiler, "-O1", "-shared", "-fPIC", "-Wl,--build-id",
			str(src), "-o", str(out)], check=True)
	return out


def generate(gen, source, symbol, binary, *extra):
	with tempfile.TemporaryDirectory() as tmp:
		out = Path(tmp) / "out"
		run = subprocess.run([sys.executable, "-I", str(gen), "--input", str(source),
				      "--symbol", symbol, "--output", str(out),
				      "--binary", str(binary), *extra],
				     capture_output=True, text=True)
		data = out.read_bytes() if run.returncode == 0 else None
	return run, data


def asm_func(name, *lines):
	body = "\\n\\t".join(lines)
	return (f'__attribute__((naked)) void {name}(void)\n'
		f'{{ __asm__ volatile("{body}\\n\\tret"); }}\n')


class NodeOffsets(unittest.TestCase):
	SOURCE = NODE / "node2shell-brotli-guard.bpf.c"
	FUNCS = {
		"DecodeLiteralBlockSwitch": 0, "SafeDecodeLiteralBlockSwitch": 0,
		"DecodeCommandBlockSwitch": 1, "SafeDecodeCommandBlockSwitch": 1,
		"DecodeDistanceBlockSwitch": 2, "SafeDecodeDistanceBlockSwitch": 2,
	}

	def fake_node(self, tmp, base, decoy=True, skew=None):
		parts = []
		for name, tree in self.FUNCS.items():
			off = base + 8 * tree + (8 if name == skew else 0)
			lines = ["push %rbx", "mov %rdi, %rbx"]
			if decoy:	# the bit reader, read before the field in v26
				lines.append("mov 0x100(%rdi), %r10")
			lines += [f"mov {off:#x}(%rdi), %r12", "cmp $0x1, %r12", "pop %rbx"]
			parts.append(asm_func(name, *lines))
		return build(tmp, "node", "".join(parts))

	def test_reads_three_trees(self):
		for base in (0x138, 0xf0):
			with tempfile.TemporaryDirectory() as tmp:
				run, data = generate(NODE / "gen_offsets.py", self.SOURCE,
						     "brotli_num_block_types",
						     self.fake_node(tmp, base))
			self.assertEqual(run.returncode, 0, run.stderr)
			self.assertEqual(struct.unpack("<3I", data),
					 (base, base + 8, base + 16))

	def test_refuses_trees_that_disagree(self):
		with tempfile.TemporaryDirectory() as tmp:
			run, _ = generate(NODE / "gen_offsets.py", self.SOURCE,
					  "brotli_num_block_types",
					  self.fake_node(tmp, 0x138, skew="SafeDecodeCommandBlockSwitch"))
		self.assertNotEqual(run.returncode, 0)
		self.assertIn("no single num_block_types offset", run.stderr)

	def test_refuses_missing_function(self):
		with tempfile.TemporaryDirectory() as tmp:
			binary = self.fake_node(tmp, 0x138)
			run, _ = generate(NODE / "gen_offsets.py",
					  NODE / "node2shell-brotli-guard-v24.bpf.c",
					  "brotli_num_block_types", binary)
		self.assertNotEqual(run.returncode, 0)
		self.assertIn("SafeDecodeCommandBlockSwitch.constprop.0", run.stderr)

	def test_real_node(self):
		node = os.environ.get("NODE2SHELL_NODE")
		if not node:
			raise unittest.SkipTest("NODE2SHELL_NODE not set")
		source = Path(os.environ.get("NODE2SHELL_SOURCE", self.SOURCE))
		run, data = generate(NODE / "gen_offsets.py", source,
				     "brotli_num_block_types", node)
		self.assertEqual(run.returncode, 0, run.stderr)
		lit, cmd, dist = struct.unpack("<3I", data)
		self.assertEqual((cmd - lit, dist - lit), (8, 16))
		print(f"{node}: num_block_types at {lit:#x}")


class NginxOffsets(unittest.TestCase):
	SOURCE = NGINX / "nginx2shell-h2-window-guard.bpf.c"

	def fake_nginx(self, tmp, off, reg="%rax", close_load="mov 0x8(%rdi), %rbx",
		       recv=0x7fffffff):
		init = asm_func("ngx_http_v2_init",
				f"movq $0x0, 0x18({reg})",
				f"movq $0xffff, {off:#x}({reg})",
				f"movq ${recv:#x}, {off + 8:#x}({reg})",
				f"movq $0xffff, {off + 16:#x}({reg})",
				# another default window of the same value, not a triple
				"movq $0xffff, 0x200(%rcx)")
		close = asm_func("ngx_http_v2_close_stream", "push %rbx", close_load,
				 "pop %rbx")
		return build(tmp, "nginx", init + close)

	def check(self, binary, *extra):
		return generate(NGINX / "gen_offsets.py", self.SOURCE,
				"nginx_h2c_send_window", binary, *extra)

	def test_reads_send_window(self):
		# disp8 and disp32, a REX.B base, a SIB base, a plain base
		for off, reg in ((0x50, "%rax"), (0x48, "%r13"), (0x60, "%r12"),
				 (0x60, "%rsp"), (0x1e0, "%rbx")):
			with tempfile.TemporaryDirectory() as tmp:
				run, data = self.check(self.fake_nginx(tmp, off, reg))
			self.assertEqual(run.returncode, 0, run.stderr)
			self.assertEqual(struct.unpack("<I", data)[0], off, reg)

	def test_refuses_without_triple(self):
		with tempfile.TemporaryDirectory() as tmp:
			run, _ = self.check(self.fake_nginx(tmp, 0x50, recv=0xffff))
		self.assertNotEqual(run.returncode, 0)
		self.assertIn("pass --send-window", run.stderr)

	def test_override(self):
		with tempfile.TemporaryDirectory() as tmp:
			binary = self.fake_nginx(tmp, 0x50, recv=0xffff)
			run, data = self.check(binary, "--send-window", "0x58")
			self.assertEqual(run.returncode, 0, run.stderr)
			self.assertEqual(struct.unpack("<I", data)[0], 0x58)
			run, _ = self.check(binary, "--send-window", "0x5")
			self.assertNotEqual(run.returncode, 0)

	def test_refuses_other_stream_layout(self):
		with tempfile.TemporaryDirectory() as tmp:
			run, _ = self.check(self.fake_nginx(tmp, 0x50,
							   close_load="mov 0x10(%rdi), %rbx"))
		self.assertNotEqual(run.returncode, 0)
		self.assertIn("stream->connection", run.stderr)

	def test_real_nginx(self):
		nginx = os.environ.get("NGINX2SHELL_NGINX")
		if not nginx:
			raise unittest.SkipTest("NGINX2SHELL_NGINX not set")
		run, data = self.check(nginx)
		self.assertEqual(run.returncode, 0, run.stderr)
		print(f"{nginx}: send_window at {struct.unpack('<I', data)[0]:#x}")


if __name__ == "__main__":
	unittest.main()

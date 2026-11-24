#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 Authors of shirudo
"""Generate sandbox_policy of app-sandbox as a raw BTF variable value.

The rules of each sandbox come from the input, the policy bit of each
from the [[upatches.policy]] table of the Beefile that has its name.
"""

import argparse
import ipaddress
from pathlib import Path
import struct
import tomllib

SANDBOX_MAX = 32
NET_MAX = 128
FIM_MAX = 64
GLOB_MAX = 32
OPS = {"create": 0x1, "rename": 0x2, "write": 0x4}
SANDBOX_KEYS = {"exec", "network", "deny"}
MAPPED_V4 = ipaddress.ip_network("::ffff:0:0/96")


def strings(value, where):
	if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
		raise ValueError(f"{where}: expected an array of strings")
	return value


def policy_bits(beefile):
	"""The bit of every policy ID the Beefile declares."""
	with beefile.open("rb") as stream:
		upatches = tomllib.load(stream).get("upatches", [])
	bits = {}
	for upatch in upatches:
		for policy in upatch.get("policy", []):
			name, bit = policy.get("id"), policy.get("bit")
			# shirudo reads a bit as hexadecimal, TOML reads it as
			# decimal without 0x: only a one-hot value is the same bit
			# to both.
			if not isinstance(bit, int) or bit <= 0 or bit & (bit - 1) or bit >> 64:
				raise ValueError(f"{beefile}: policy {name}: write the bit as "
						 "one-hot hexadecimal with 0x")
			if bits.setdefault(name, bit) != bit:
				raise ValueError(f"{beefile}: policy {name} has two bits")
	return bits


def network_rule(text, where):
	try:
		network = ipaddress.ip_network(text, strict=False)
	except ValueError as error:
		raise ValueError(f"{where}: {error}") from error
	if network.version == 4:
		address = int(MAPPED_V4.network_address) | int(network.network_address)
		network = ipaddress.ip_network((address, 96 + network.prefixlen))
	return network.network_address.packed, network.netmask.packed


def networks(names, sets, where):
	rules = []
	for name in strings(names, where):
		if name.startswith("@"):
			if name[1:] not in sets:
				raise ValueError(f"{where}: no [networks] entry {name[1:]}")
			members = strings(sets[name[1:]], f"networks.{name[1:]}")
		else:
			members = [name]
		rules.extend(network_rule(member, where) for member in members)
	return rules


def glob_rule(pattern, where):
	raw = pattern.encode()
	if not raw or b"/" in raw or b"\0" in raw or raw.count(b"*") > 1:
		raise ValueError(f"{where}: {pattern!r} is not a file name with at "
				 "most one '*'")
	prefix, star, suffix = raw.partition(b"*")
	if len(prefix) > GLOB_MAX or len(suffix) > GLOB_MAX:
		raise ValueError(f"{where}: {pattern!r} has more than {GLOB_MAX} "
				 "bytes on one side of its '*'")
	return prefix, suffix, bool(star)


def read_policy(path, bits):
	with path.open("rb") as stream:
		config = tomllib.load(stream)
	unknown = set(config) - {"networks", "sandbox"}
	if unknown:
		raise ValueError(f"{path}: unknown table {sorted(unknown)[0]}")
	sets = config.get("networks", {})
	policy = {"sandboxes": 0, "exec_enforced": 0, "net_enforced": 0,
		  "exec": [], "net": {}, "fim": {}}
	for name, sandbox in config.get("sandbox", {}).items():
		where = f"{path}: sandbox.{name}"
		if name not in bits:
			raise ValueError(f"{where}: the Beefile has no policy {name}")
		unknown = set(sandbox) - SANDBOX_KEYS
		if unknown:
			raise ValueError(f"{where}: unknown key {sorted(unknown)[0]}")
		bit = bits[name]
		policy["sandboxes"] |= bit
		if "exec" in sandbox:
			allowed = bit
			for other in strings(sandbox["exec"], f"{where}.exec"):
				if other not in bits:
					raise ValueError(f"{where}.exec: the Beefile has no "
							 f"policy {other}")
				allowed |= bits[other]
			policy["exec_enforced"] |= bit
			policy["exec"].append((bit, allowed))
		if "network" in sandbox:
			policy["net_enforced"] |= bit
			for rule in networks(sandbox["network"], sets, f"{where}.network"):
				policy["net"][rule] = policy["net"].get(rule, 0) | bit
		deny = sandbox.get("deny", {})
		unknown = set(deny) - set(OPS)
		if unknown:
			raise ValueError(f"{where}.deny: unknown key {sorted(unknown)[0]}")
		for op, patterns in deny.items():
			for pattern in strings(patterns, f"{where}.deny.{op}"):
				rule = glob_rule(pattern, f"{where}.deny.{op}")
				sandboxes, ops = policy["fim"].get(rule, (0, 0))
				policy["fim"][rule] = (sandboxes | bit, ops | OPS[op])
	return policy


def table(entries, limit, size, what):
	if len(entries) > limit:
		raise ValueError(f"{len(entries)} {what}; the object holds {limit}")
	return b"".join(entries).ljust(limit * size, b"\0")


def encode(policy):
	"""struct sandbox_policy of app-sandbox.bpf.c, little-endian."""
	execs = [struct.pack("<QQ", bit, allowed) for bit, allowed in policy["exec"]]
	nets = [struct.pack("<Q16s16s", sandboxes, address, mask)
		for (address, mask), sandboxes in sorted(policy["net"].items())]
	fims = [struct.pack(f"<QBBBB4x{GLOB_MAX}s{GLOB_MAX}s", sandboxes, ops,
			    len(prefix), len(suffix), wildcard, prefix, suffix)
		for (prefix, suffix, wildcard), (sandboxes, ops)
		in sorted(policy["fim"].items())]
	header = struct.pack("<QQQIII4x", policy["sandboxes"],
			     policy["exec_enforced"], policy["net_enforced"],
			     len(execs), len(nets), len(fims))
	return (header + table(execs, SANDBOX_MAX, 16, "sandboxes with exec rules") +
		table(nets, NET_MAX, 40, "networks") +
		table(fims, FIM_MAX, 80, "file name patterns"))


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, type=Path)
	parser.add_argument("--symbol", required=True)
	parser.add_argument("--output", required=True, type=Path)
	parser.add_argument("--beefile", type=Path, default=Path("Beefile"))
	args = parser.parse_args()
	try:
		if args.symbol != "sandbox_policy":
			raise ValueError(f"unsupported symbol: {args.symbol}")
		policy = read_policy(args.input, policy_bits(args.beefile))
		args.output.write_bytes(encode(policy))
	except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
		parser.error(str(error))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

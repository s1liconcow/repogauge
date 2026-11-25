// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * Node2Shell Brotli guard for an official Node.js 22 or 26 build at
 * /usr/local/bin/node, where the official images and the release
 * tarballs unpacked into /usr/local put it. v22.23.3 and v26.12.0, the
 * last builds before the fix, name all six block switch functions.
 * node2shell-brotli-guard.h holds the program; README.md says what it
 * does. gen_offsets.py reads NODE2SHELL_PATH and the NODE2SHELL_GUARD()
 * calls from this file.
 */

#define NODE2SHELL_PATH	"/usr/local/bin/node"
#include "node2shell-brotli-guard.h"

NODE2SHELL_GUARD(node2shell_literal,
		 "DecodeLiteralBlockSwitch", TREE_LITERAL)
NODE2SHELL_GUARD(node2shell_command,
		 "DecodeCommandBlockSwitch", TREE_COMMAND)
NODE2SHELL_GUARD(node2shell_distance,
		 "DecodeDistanceBlockSwitch", TREE_DISTANCE)
NODE2SHELL_GUARD(node2shell_safe_literal,
		 "SafeDecodeLiteralBlockSwitch", TREE_LITERAL)
NODE2SHELL_GUARD(node2shell_safe_command,
		 "SafeDecodeCommandBlockSwitch", TREE_COMMAND)
NODE2SHELL_GUARD(node2shell_safe_distance,
		 "SafeDecodeDistanceBlockSwitch", TREE_DISTANCE)

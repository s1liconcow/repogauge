// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * Node2Shell Brotli guard for an official Node.js 24 build at
 * /usr/local/bin/node. In v24.22.0, the last build before the fix, GCC
 * cloned the safe command and distance block switch functions as
 * .constprop.0, and those clones are what the decoder calls.
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
		 "SafeDecodeCommandBlockSwitch.constprop.0", TREE_COMMAND)
NODE2SHELL_GUARD(node2shell_safe_distance,
		 "SafeDecodeDistanceBlockSwitch.constprop.0", TREE_DISTANCE)

// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * PHP2Shell OOM guard for the PHP CLI at /usr/local/bin/php, where a
 * build from source and the official php images install it.
 * php2shell-oom-guard.h holds the program; README.md says what it does.
 */

#define PHP2SHELL_PATH	"/usr/local/bin/php"
#include "php2shell-oom-guard.h"

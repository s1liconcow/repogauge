// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * PHP2Shell OOM guard for mod_php 8 at
 * /usr/lib/apache2/modules/libphp.so, as the official php:*-apache
 * images install it. A uprobe on a shared object runs in every process
 * that maps it, here the Apache children.
 * php2shell-oom-guard.h holds the program; README.md says what it does.
 */

#define PHP2SHELL_PATH	"/usr/lib/apache2/modules/libphp.so"
#include "php2shell-oom-guard.h"

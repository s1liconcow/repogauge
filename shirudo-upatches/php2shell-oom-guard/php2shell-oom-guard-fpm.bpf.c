// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * PHP2Shell OOM guard for PHP-FPM at /usr/local/sbin/php-fpm, where a
 * build from source and the official php:*-fpm images install it.
 * php2shell-oom-guard.h holds the program; README.md says what it does.
 */

#define PHP2SHELL_PATH	"/usr/local/sbin/php-fpm"
#include "php2shell-oom-guard.h"

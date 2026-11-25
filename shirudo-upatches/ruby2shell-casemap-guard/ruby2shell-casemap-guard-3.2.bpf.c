// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * Ruby2Shell case_map guard for Ruby 3.2 at /usr/local, where a build
 * from source and the official ruby:3.2 images install it, with one
 * enc/<encoding>.so per encoding. ruby2shell-casemap-guard.h holds the
 * program; README.md says what it does.
 */

#define RUBY2SHELL_ENC_DIR	"/usr/local/lib/ruby/3.2.0/x86_64-linux/enc"
#include "ruby2shell-casemap-guard.h"

RUBY2SHELL_GUARD_ALL()

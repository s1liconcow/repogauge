// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * Copyright (c) 2026 Authors of shirudo
 *
 * app-sandbox: a task that carries a sandbox's policy bit executes only
 * the files its sandbox names, connects only to the networks it names,
 * and does not create, rename or open for writing a file whose name
 * matches a pattern it denies. gen_policy.py writes sandbox_policy.
 */
#include "vmlinux.h"
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "policy.h"

char _license[] SEC("license") = "Dual BSD/GPL";

#define SANDBOX_MAX		32
#define SANDBOX_NET_MAX		128
#define SANDBOX_FIM_MAX		64
#define SANDBOX_GLOB_MAX	32

#define SANDBOX_FIM_CREATE	0x1
#define SANDBOX_FIM_RENAME	0x2
#define SANDBOX_FIM_WRITE	0x4

#define EACCES			13
#define ENODATA			61
#define EOPNOTSUPP		95
#define AF_UNSPEC		0
#define AF_INET			2
#define AF_INET6		10
#define AF_PACKET		17
#define SOCK_RAW		3
#define SOCK_TYPE_MASK		0xf
#define FMODE_WRITE		0x2
#define ATTR_SIZE		0x8
#define NAME_MAX		255

/* gen_policy.py packs these structs byte for byte. */
struct sandbox_exec_rule {
	__u64	sandbox;
	__u64	allowed;
};

/* An address in network byte order, IPv4 as ::ffff:a.b.c.d. */
union sandbox_addr {
	__u8	bytes[16];
	__be32	words[4];
	__u64	halves[2];
};

struct sandbox_net_rule {
	__u64	sandboxes;
	__u64	addr[2];
	__u64	mask[2];
};

struct sandbox_fim_rule {
	__u64	sandboxes;
	__u8	ops;
	__u8	prefix_len;
	__u8	suffix_len;
	__u8	wildcard;
	__u8	reserved[4];
	__u8	prefix[SANDBOX_GLOB_MAX];
	__u8	suffix[SANDBOX_GLOB_MAX];
};

struct sandbox_policy {
	__u64	sandboxes;
	__u64	exec_enforced;
	__u64	net_enforced;
	__u32	nr_exec;
	__u32	nr_net;
	__u32	nr_fim;
	__u32	reserved;
	struct sandbox_exec_rule	exec[SANDBOX_MAX];
	struct sandbox_net_rule		net[SANDBOX_NET_MAX];
	struct sandbox_fim_rule		fim[SANDBOX_FIM_MAX];
};

const volatile struct sandbox_policy sandbox_policy = {};

struct sandbox_label {
	__u64	bits;
};

/* bpf_dynptr_from_mem() takes map memory only, and the exec hook sleeps. */
struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct sandbox_label);
} sandbox_label SEC(".maps");

/* The first and the last bytes of a file name, all a pattern compares. */
struct sandbox_name {
	__u32	len;
	__u32	tail_len;
	__u8	head[SANDBOX_GLOB_MAX];
	__u8	tail[SANDBOX_GLOB_MAX];
};

int bpf_get_file_xattr(struct file *file, const char *name__str,
		       struct bpf_dynptr *value_p) __ksym;

static __always_inline __u64 current_sandboxes(void)
{
	struct task_policy *policy;

	policy = bpf_task_storage_get(&shirudo_task_policy,
				      bpf_get_current_task_btf(), NULL, 0);
	return policy ? policy->active & sandbox_policy.sandboxes : 0;
}

static __always_inline int file_policy_bits(struct file *file, __u64 *bits)
{
	struct sandbox_label *label;
	struct bpf_dynptr value;
	int len;

	label = bpf_task_storage_get(&sandbox_label,
				     bpf_get_current_task_btf(), NULL,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!label ||
	    bpf_dynptr_from_mem(&label->bits, sizeof(label->bits), 0, &value))
		return -EACCES;
	len = bpf_get_file_xattr(file, XATTR_POLICY, &value);
	if (len == -ENODATA || len == -EOPNOTSUPP) {
		*bits = 0;
		return 0;
	}
	if (len != sizeof(label->bits))
		return -EACCES;
	*bits = label->bits;
	return 0;
}

static __always_inline bool exec_allowed(__u64 sandboxes, __u64 file_bits)
{
	const volatile struct sandbox_exec_rule *rule;
	__u32 nr_exec = sandbox_policy.nr_exec;
	__u32 i;

	for (i = 0; i < SANDBOX_MAX; i++) {
		if (i >= nr_exec)
			break;
		rule = &sandbox_policy.exec[i];
		if ((sandboxes & rule->sandbox) && !(file_bits & rule->allowed))
			return false;
	}
	return true;
}

/* The kernel calls it for a script and again for each interpreter. */
SEC("lsm.s/bprm_check_security")
int BPF_PROG(sandbox_exec, struct linux_binprm *bprm, int ret)
{
	__u64 sandboxes, file_bits;

	if (ret)
		return ret;
	sandboxes = current_sandboxes() & sandbox_policy.exec_enforced;
	if (!sandboxes)
		return 0;
	if (!bprm->file || file_policy_bits(bprm->file, &file_bits))
		return -EACCES;
	return exec_allowed(sandboxes, file_bits) ? 0 : -EACCES;
}

static __always_inline bool net_allowed(__u64 sandboxes,
					const union sandbox_addr *addr)
{
	const volatile struct sandbox_net_rule *rule;
	__u32 nr_net = sandbox_policy.nr_net;
	__u64 allowed = 0;
	__u32 i;

	for (i = 0; i < SANDBOX_NET_MAX; i++) {
		if (i >= nr_net)
			break;
		rule = &sandbox_policy.net[i];
		if ((addr->halves[0] & rule->mask[0]) == rule->addr[0] &&
		    (addr->halves[1] & rule->mask[1]) == rule->addr[1])
			allowed |= rule->sandboxes;
	}
	return !(sandboxes & ~allowed);
}

static __always_inline int peer_verdict(__u64 sandboxes, __u16 family,
					const struct sockaddr *address,
					int addrlen)
{
	union sandbox_addr addr = {};
	struct sockaddr_in6 sin6;
	struct sockaddr_in sin;

	if (family == AF_INET) {
		if (addrlen < (int)sizeof(sin) ||
		    bpf_probe_read_kernel(&sin, sizeof(sin), address))
			return -EACCES;
		addr.words[2] = bpf_htonl(0xffff);
		addr.words[3] = sin.sin_addr.s_addr;
	} else if (family == AF_INET6) {
		if (addrlen < (int)sizeof(sin6) ||
		    bpf_probe_read_kernel(&sin6, sizeof(sin6), address))
			return -EACCES;
		__builtin_memcpy(addr.bytes, &sin6.sin6_addr,
				 sizeof(addr.bytes));
	} else {
		return 0;
	}
	return net_allowed(sandboxes, &addr) ? 0 : -EACCES;
}

SEC("lsm/socket_connect")
int BPF_PROG(sandbox_connect, struct socket *sock, struct sockaddr *address,
	     int addrlen, int ret)
{
	__u64 sandboxes;
	__u16 family;

	if (ret)
		return ret;
	sandboxes = current_sandboxes() & sandbox_policy.net_enforced;
	if (!sandboxes)
		return 0;
	if (!address || addrlen < (int)sizeof(family) ||
	    bpf_probe_read_kernel(&family, sizeof(family),
				  &address->sa_family))
		return -EACCES;
	return peer_verdict(sandboxes, family, address, addrlen);
}

SEC("lsm/socket_sendmsg")
int BPF_PROG(sandbox_sendmsg, struct socket *sock, struct msghdr *msg,
	     int size, int ret)
{
	struct sockaddr *address;
	__u64 sandboxes;
	struct sock *sk;
	__u16 family;
	int addrlen;

	if (ret)
		return ret;
	address = msg->msg_name;
	if (!address)
		return 0;
	sandboxes = current_sandboxes() & sandbox_policy.net_enforced;
	if (!sandboxes)
		return 0;
	addrlen = msg->msg_namelen;
	if (addrlen < (int)sizeof(family) ||
	    bpf_probe_read_kernel(&family, sizeof(family),
				  &address->sa_family))
		return -EACCES;
	/* IPv4 sends to an AF_UNSPEC address as to an AF_INET one. */
	sk = sock->sk;
	if (family == AF_UNSPEC && sk && sk->__sk_common.skc_family == AF_INET)
		family = AF_INET;
	return peer_verdict(sandboxes, family, address, addrlen);
}

/* A raw or packet socket writes its own headers, past both hooks above. */
SEC("lsm/socket_create")
int BPF_PROG(sandbox_socket_create, int family, int type, int protocol,
	     int kern, int ret)
{
	if (ret || kern)
		return ret;
	if (family != AF_PACKET &&
	    !((family == AF_INET || family == AF_INET6) &&
	      (type & SOCK_TYPE_MASK) == SOCK_RAW))
		return 0;
	return current_sandboxes() & sandbox_policy.net_enforced ? -EACCES : 0;
}

static __always_inline int read_name(struct dentry *dentry,
				     struct sandbox_name *name)
{
	const unsigned char *bytes = dentry->d_name.name;
	__u32 len = dentry->d_name.len;
	__u32 part_len;

	if (!len || len > NAME_MAX)
		return -EACCES;
	part_len = len;
	if (part_len > SANDBOX_GLOB_MAX)
		part_len = SANDBOX_GLOB_MAX;
	if (bpf_probe_read_kernel(name->head, part_len, bytes) ||
	    bpf_probe_read_kernel(name->tail, part_len, bytes + len - part_len))
		return -EACCES;
	name->len = len;
	name->tail_len = part_len;
	return 0;
}

static __always_inline bool byte_matches(__u8 pattern, __u8 byte)
{
	return pattern == '?' || pattern == byte;
}

static __always_inline bool
name_matches(const volatile struct sandbox_fim_rule *rule,
	     const struct sandbox_name *name)
{
	__u32 prefix_len = rule->prefix_len;
	__u32 suffix_len = rule->suffix_len;
	__u32 i, tail_at;

	if (prefix_len > SANDBOX_GLOB_MAX || suffix_len > SANDBOX_GLOB_MAX)
		return true;
	if (rule->wildcard ? name->len < prefix_len + suffix_len :
			     name->len != prefix_len)
		return false;
	for (i = 0; i < SANDBOX_GLOB_MAX; i++) {
		if (i >= prefix_len)
			break;
		if (!byte_matches(rule->prefix[i], name->head[i]))
			return false;
	}
	tail_at = name->tail_len - suffix_len;
	for (i = 0; i < SANDBOX_GLOB_MAX; i++) {
		if (i >= suffix_len)
			break;
		if (!byte_matches(rule->suffix[i],
				  name->tail[(tail_at + i) &
					     (SANDBOX_GLOB_MAX - 1)]))
			return false;
	}
	return true;
}

static __noinline bool name_denied(__u64 sandboxes, __u32 op,
				   struct dentry *dentry)
{
	const volatile struct sandbox_fim_rule *rule;
	__u32 nr_fim = sandbox_policy.nr_fim;
	struct sandbox_name name = {};
	__u32 i;

	if (!dentry || read_name(dentry, &name))
		return true;
	for (i = 0; i < SANDBOX_FIM_MAX; i++) {
		if (i >= nr_fim)
			break;
		rule = &sandbox_policy.fim[i];
		if ((rule->sandboxes & sandboxes) && (rule->ops & op) &&
		    name_matches(rule, &name))
			return true;
	}
	return false;
}

static __always_inline int name_verdict(__u32 op, struct dentry *dentry)
{
	__u64 sandboxes = current_sandboxes();

	if (!sandboxes)
		return 0;
	return name_denied(sandboxes, op, dentry) ? -EACCES : 0;
}

SEC("lsm/inode_create")
int BPF_PROG(sandbox_create, struct mnt_idmap *idmap, struct inode *dir,
	     struct dentry *dentry, umode_t mode, int ret)
{
	return ret ? ret : name_verdict(SANDBOX_FIM_CREATE, dentry);
}

SEC("lsm/inode_mkdir")
int BPF_PROG(sandbox_mkdir, struct mnt_idmap *idmap, struct inode *dir,
	     struct dentry *dentry, umode_t mode, int ret)
{
	return ret ? ret : name_verdict(SANDBOX_FIM_CREATE, dentry);
}

SEC("lsm/inode_symlink")
int BPF_PROG(sandbox_symlink, struct mnt_idmap *idmap, struct inode *dir,
	     struct dentry *dentry, const char *old_name, int ret)
{
	return ret ? ret : name_verdict(SANDBOX_FIM_CREATE, dentry);
}

SEC("lsm/inode_mknod")
int BPF_PROG(sandbox_mknod, struct mnt_idmap *idmap, struct inode *dir,
	     struct dentry *dentry, umode_t mode, dev_t dev, int ret)
{
	return ret ? ret : name_verdict(SANDBOX_FIM_CREATE, dentry);
}

SEC("lsm/inode_link")
int BPF_PROG(sandbox_link, struct mnt_idmap *idmap, struct dentry *old_dentry,
	     struct inode *dir, struct dentry *new_dentry, int ret)
{
	return ret ? ret : name_verdict(SANDBOX_FIM_CREATE, new_dentry);
}

SEC("lsm/inode_rename")
int BPF_PROG(sandbox_rename, struct inode *old_dir, struct dentry *old_dentry,
	     struct inode *new_dir, struct dentry *new_dentry, int ret)
{
	__u64 sandboxes;

	if (ret)
		return ret;
	sandboxes = current_sandboxes();
	if (!sandboxes)
		return 0;
	if (name_denied(sandboxes, SANDBOX_FIM_RENAME, old_dentry) ||
	    name_denied(sandboxes, SANDBOX_FIM_RENAME, new_dentry))
		return -EACCES;
	return 0;
}

SEC("lsm/file_open")
int BPF_PROG(sandbox_open, struct file *file, int ret)
{
	if (ret || !(file->f_mode & FMODE_WRITE))
		return ret;
	return name_verdict(SANDBOX_FIM_WRITE, file->f_path.dentry);
}

SEC("lsm/inode_setattr")
int BPF_PROG(sandbox_truncate, struct mnt_idmap *idmap, struct dentry *dentry,
	     struct iattr *attr, int ret)
{
	if (ret || !(attr->ia_valid & ATTR_SIZE))
		return ret;
	return name_verdict(SANDBOX_FIM_WRITE, dentry);
}

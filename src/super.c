// SPDX-License-Identifier: GPL-2.0-only
#include <linux/fs_context.h>
#include <linux/fs_parser.h>
#include <linux/magic.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/pagemap.h>
#include <linux/seq_file.h>
#include <linux/statfs.h>
#include <linux/user_namespace.h>
#include "prefixfs.h"

static struct kmem_cache *pf_inode_cache;
static struct file_system_type pf_type;

bool pf_matches(struct pf_sb *sbi, const struct qstr *name)
{
	struct pf_prefix *prefix;

	list_for_each_entry(prefix, &sbi->prefixes, list)
		if (name->len >= prefix->len &&
		    !memcmp(name->name, prefix->name, prefix->len))
			return true;
	return false;
}

void pf_refresh(struct inode *inode)
{
	struct inode *lower = PF_I(inode)->lower;

	fsstack_copy_attr_all(inode, lower);
	fsstack_copy_inode_size(inode, lower);
	/* Privileged executable semantics are intentionally not exposed. */
	if (S_ISREG(inode->i_mode))
		inode->i_mode &= ~(S_ISUID | S_ISGID);
}

static struct inode *pf_alloc_inode(struct super_block *sb)
{
	struct pf_inode *pi = alloc_inode_sb(sb, pf_inode_cache, GFP_KERNEL);

	if (!pi)
		return NULL;
	pi->lower = NULL;
	return &pi->inode;
}

static void pf_free_inode(struct inode *inode)
{
	kmem_cache_free(pf_inode_cache, PF_I(inode));
}

static void pf_evict_inode(struct inode *inode)
{
	truncate_inode_pages_final(&inode->i_data);
	clear_inode(inode);
	iput(PF_I(inode)->lower);
}

static int pf_inode_test(struct inode *inode, void *data)
{
	struct pf_inode *key = data;

	return PF_I(inode)->lower == key->lower &&
	       PF_I(inode)->backend == key->backend;
}

static int pf_inode_set(struct inode *inode, void *data)
{
	struct pf_inode *key = data;

	PF_I(inode)->lower = key->lower;
	PF_I(inode)->backend = key->backend;
	return 0;
}

struct inode *pf_iget(struct super_block *sb, struct inode *lower,
		     enum pf_backend backend)
{
	struct pf_inode key = { .lower = lower, .backend = backend };
	struct inode *inode;

	if (!igrab(lower))
		return ERR_PTR(-ESTALE);
	inode = iget5_locked(sb, (unsigned long)lower ^ backend,
			    pf_inode_test, pf_inode_set, &key);
	if (!inode) {
		iput(lower);
		return ERR_PTR(-ENOMEM);
	}
	if (!(inode->i_state & I_NEW)) {
		iput(lower);
		pf_refresh(inode);
		return inode;
	}
	inode->i_ino = lower->i_ino;
	pf_refresh(inode);
	if (S_ISDIR(inode->i_mode)) {
		inode->i_op = &pf_dir_iops;
		inode->i_fop = &pf_dir_ops;
	} else if (S_ISLNK(inode->i_mode)) {
		inode->i_op = &pf_symlink_iops;
	} else {
		if (!S_ISREG(inode->i_mode))
			init_special_inode(inode, inode->i_mode, lower->i_rdev);
		inode->i_op = &pf_file_iops;
		if (S_ISREG(inode->i_mode) || S_ISFIFO(inode->i_mode))
			inode->i_fop = &pf_file_ops;
	}
	unlock_new_inode(inode);
	return inode;
}

static void pf_free_sb(struct pf_sb *sbi)
{
	struct pf_prefix *prefix, *next;

	if (!sbi)
		return;
	path_put(&sbi->source);
	path_put(&sbi->memory);
	list_for_each_entry_safe(prefix, next, &sbi->prefixes, list) {
		list_del(&prefix->list);
		kfree(prefix);
	}
	kfree(sbi->memory_name);
	kfree(sbi);
}

static void pf_put_super(struct super_block *sb)
{
	pf_free_sb(PF_SB(sb));
}

static int pf_statfs(struct dentry *dentry, struct kstatfs *buf)
{
	/* Report the actual backend's space and filesystem type. */
	return vfs_statfs(&PF_D(dentry)->lower, buf);
}

static int pf_sync_one(struct super_block *sb)
{
	int error;

	down_read(&sb->s_umount);
	error = sync_filesystem(sb);
	up_read(&sb->s_umount);
	return error;
}

static int pf_sync_fs(struct super_block *sb, int wait)
{
	struct pf_sb *sbi = PF_SB(sb);
	int error, other;

	if (!wait)
		return 0;
	error = pf_sync_one(sbi->source.mnt->mnt_sb);
	if (sbi->source.mnt->mnt_sb == sbi->memory.mnt->mnt_sb)
		return error;
	other = pf_sync_one(sbi->memory.mnt->mnt_sb);
	return error ? error : other;
}

static int pf_freeze_fs(struct super_block *sb)
{
	/* Native mmap writes cannot be frozen by the wrapper superblock. */
	return -EOPNOTSUPP;
}

static int pf_show_options(struct seq_file *m, struct dentry *root)
{
	struct pf_sb *sbi = PF_SB(root->d_sb);
	struct pf_prefix *prefix;

	seq_puts(m, ",memory=");
	seq_path(m, &sbi->memory, " \t\n\\,");
	list_for_each_entry(prefix, &sbi->prefixes, list)
		seq_show_option(m, "prefix", prefix->name);
	return 0;
}

static const struct super_operations pf_super_ops = {
	.alloc_inode = pf_alloc_inode,
	.free_inode = pf_free_inode,
	.evict_inode = pf_evict_inode,
	.put_super = pf_put_super,
	.statfs = pf_statfs,
	.sync_fs = pf_sync_fs,
	.freeze_fs = pf_freeze_fs,
	.show_options = pf_show_options,
};

enum { Opt_memory, Opt_prefix };
static const struct fs_parameter_spec pf_parameters[] = {
	fsparam_string("memory", Opt_memory),
	fsparam_string("prefix", Opt_prefix),
	{}
};

static int pf_parse_param(struct fs_context *fc, struct fs_parameter *param)
{
	struct pf_sb *sbi = fc->fs_private;
	struct fs_parse_result result;
	struct pf_prefix *prefix;
	size_t len;
	int option;

	if (!strcmp(param->key, "source"))
		return vfs_parse_fs_param_source(fc, param);
	option = fs_parse(fc, pf_parameters, param, &result);
	if (option < 0)
		return option;
	if (option == Opt_memory) {
		if (sbi->memory_name)
			return -EINVAL;
		sbi->memory_name = param->string;
		param->string = NULL;
		return 0;
	}
	len = strlen(param->string);
	if (!len || len > NAME_MAX || strchr(param->string, '/') ||
	    !strcmp(param->string, ".") || !strcmp(param->string, "..") ||
	    sbi->nr_prefixes >= PF_MAX_PREFIXES)
		return -EINVAL;
	prefix = kmalloc(sizeof(*prefix) + len + 1, GFP_KERNEL);
	if (!prefix)
		return -ENOMEM;
	prefix->len = len;
	memcpy(prefix->name, param->string, len + 1);
	list_add_tail(&prefix->list, &sbi->prefixes);
	sbi->nr_prefixes++;
	return 0;
}

static int pf_fill_super(struct super_block *sb, struct fs_context *fc)
{
	struct pf_sb *sbi = fc->fs_private;
	struct inode *inode;
	int error;

	sb->s_fs_info = sbi;
	fc->fs_private = NULL;
	sb->s_op = &pf_super_ops;
	sb->s_d_op = &pf_dentry_ops;
	sb->s_magic = PREFIXFS_MAGIC;
	sb->s_flags |= SB_POSIXACL;
	sb->s_iflags |= SB_I_NODEV;
	sb->s_maxbytes = min(sbi->source.mnt->mnt_sb->s_maxbytes,
			     sbi->memory.mnt->mnt_sb->s_maxbytes);
	sb->s_blocksize = PAGE_SIZE;
	sb->s_blocksize_bits = PAGE_SHIFT;
	sb->s_time_gran = 1;
	sb->s_xattr = pf_xattr_handlers;
	sb->s_stack_depth = max(sbi->source.mnt->mnt_sb->s_stack_depth,
			       sbi->memory.mnt->mnt_sb->s_stack_depth) + 1;
	inode = pf_iget(sb, d_inode(sbi->source.dentry), PF_SOURCE);
	if (IS_ERR(inode))
		return PTR_ERR(inode);
	sb->s_root = d_make_root(inode);
	if (!sb->s_root)
		return -ENOMEM;
	error = pf_set_path(sb->s_root, &sbi->source, PF_SOURCE);
	return error;
}

static int pf_get_tree(struct fs_context *fc)
{
	struct pf_sb *sbi = fc->fs_private;
	int error;

	if (fc->user_ns != &init_user_ns)
		return -EPERM;
	if (!fc->source || !sbi->memory_name || !sbi->nr_prefixes)
		return -EINVAL;
	error = kern_path(fc->source, LOOKUP_FOLLOW | LOOKUP_DIRECTORY,
			  &sbi->source);
	if (error)
		return error;
	error = kern_path(sbi->memory_name, LOOKUP_FOLLOW | LOOKUP_DIRECTORY,
			  &sbi->memory);
	if (error)
		return error;
	if (sbi->memory.mnt->mnt_sb->s_magic != TMPFS_MAGIC ||
	    sbi->source.mnt->mnt_sb->s_type == &pf_type ||
	    is_idmapped_mnt(sbi->source.mnt) || is_idmapped_mnt(sbi->memory.mnt) ||
	    path_is_under(&sbi->source, &sbi->memory) ||
	    path_is_under(&sbi->memory, &sbi->source))
		return -EINVAL;
	if (max(sbi->source.mnt->mnt_sb->s_stack_depth,
		sbi->memory.mnt->mnt_sb->s_stack_depth) >= FILESYSTEM_MAX_STACK_DEPTH)
		return -EINVAL;
	return get_tree_nodev(fc, pf_fill_super);
}

static void pf_free_context(struct fs_context *fc)
{
	pf_free_sb(fc->fs_private);
}

static const struct fs_context_operations pf_context_ops = {
	.free = pf_free_context,
	.parse_param = pf_parse_param,
	.get_tree = pf_get_tree,
};

static int pf_init_context(struct fs_context *fc)
{
	struct pf_sb *sbi = kzalloc(sizeof(*sbi), GFP_KERNEL);

	if (!sbi)
		return -ENOMEM;
	INIT_LIST_HEAD(&sbi->prefixes);
	fc->fs_private = sbi;
	fc->ops = &pf_context_ops;
	return 0;
}

static struct file_system_type pf_type = {
	.owner = THIS_MODULE,
	.name = "prefixfs",
	.init_fs_context = pf_init_context,
	.kill_sb = kill_anon_super,
};

static void pf_inode_init(void *object)
{
	inode_init_once(&((struct pf_inode *)object)->inode);
}

static int __init pf_init(void)
{
	int error;

	pf_inode_cache = kmem_cache_create("prefixfs_inode", sizeof(struct pf_inode),
			0, SLAB_RECLAIM_ACCOUNT | SLAB_ACCOUNT, pf_inode_init);
	if (!pf_inode_cache)
		return -ENOMEM;
	error = register_filesystem(&pf_type);
	if (error)
		kmem_cache_destroy(pf_inode_cache);
	return error;
}

static void __exit pf_exit(void)
{
	unregister_filesystem(&pf_type);
	rcu_barrier();
	kmem_cache_destroy(pf_inode_cache);
}

module_init(pf_init);
module_exit(pf_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Directory-prefix routing to native source and tmpfs backends");
MODULE_ALIAS_FS("prefixfs");

/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _PREFIXFS_H
#define _PREFIXFS_H

#include <linux/fs.h>
#include <linux/fs_stack.h>
#include <linux/mount.h>
#include <linux/path.h>
#include <linux/rbtree.h>
#include <linux/slab.h>

#define PREFIXFS_MAGIC 0x70726678
#define PF_MAX_PREFIXES 64

enum pf_backend { PF_SOURCE, PF_MEMORY };

struct pf_prefix {
	struct list_head list;
	unsigned int len;
	char name[];
};

struct pf_sb {
	struct path source;
	struct path memory;
	char *memory_name;
	struct list_head prefixes;
	unsigned int nr_prefixes;
};

struct pf_inode {
	struct inode *lower;
	enum pf_backend backend;
	struct inode inode;
};

struct pf_dentry {
	struct path lower;
	enum pf_backend backend;
};

struct pf_dirent {
	struct rb_node node;
	u64 ino;
	unsigned int type;
	unsigned int len;
	bool needs_lookup;
	char name[];
};

struct pf_snapshot {
	struct rb_root names;
	struct pf_dirent **entries;
	unsigned int count;
};

struct pf_file {
	struct file *lower;
	struct file *memory;
	struct pf_snapshot *snapshot;
};

static inline struct pf_sb *PF_SB(const struct super_block *sb)
{
	return sb->s_fs_info;
}

static inline struct pf_inode *PF_I(const struct inode *inode)
{
	return container_of(inode, struct pf_inode, inode);
}

static inline struct pf_dentry *PF_D(const struct dentry *dentry)
{
	return dentry->d_fsdata;
}

static inline struct pf_file *PF_F(const struct file *file)
{
	return file->private_data;
}

bool pf_matches(struct pf_sb *sbi, const struct qstr *name);
void pf_refresh(struct inode *inode);
struct inode *pf_iget(struct super_block *sb, struct inode *lower,
		     enum pf_backend backend);
int pf_set_path(struct dentry *dentry, const struct path *path,
		enum pf_backend backend);
int pf_lookup_backing(struct dentry *parent, const struct qstr *name,
		      struct path *result, enum pf_backend *backend);
void pf_parent_path(struct dentry *parent, enum pf_backend backend,
		    struct path *result);
int pf_file_open(struct inode *inode, struct file *file);
int pf_file_release(struct inode *inode, struct file *file);
int pf_root_iterate(struct file *file, struct dir_context *ctx);
void pf_snapshot_free(struct pf_snapshot *snapshot);

extern const struct dentry_operations pf_dentry_ops;
extern const struct inode_operations pf_dir_iops;
extern const struct inode_operations pf_file_iops;
extern const struct inode_operations pf_symlink_iops;
extern const struct file_operations pf_file_ops;
extern const struct file_operations pf_dir_ops;
extern const struct xattr_handler * const pf_xattr_handlers[];

#endif

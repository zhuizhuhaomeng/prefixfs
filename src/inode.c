// SPDX-License-Identifier: GPL-2.0-only
#include <linux/cred.h>
#include <linux/namei.h>
#include <linux/posix_acl.h>
#include <linux/posix_acl_xattr.h>
#include <linux/xattr.h>
#include "prefixfs.h"

int pf_set_path(struct dentry *dentry, const struct path *path,
		enum pf_backend backend)
{
	struct pf_dentry *pd = PF_D(dentry);

	if (!pd) {
		pd = kzalloc(sizeof(*pd), GFP_NOFS);
		if (!pd)
			return -ENOMEM;
		dentry->d_fsdata = pd;
	}
	path_get(path);
	path_put(&pd->lower);
	pd->lower = *path;
	pd->backend = backend;
	return 0;
}

static void pf_d_release(struct dentry *dentry)
{
	struct pf_dentry *pd = PF_D(dentry);

	if (pd) {
		path_put(&pd->lower);
		kfree(pd);
	}
}

static int pf_d_revalidate(struct dentry *dentry, unsigned int flags)
{
	struct dentry *lower;
	int valid;

	if (flags & LOOKUP_RCU)
		return -ECHILD;
	if (IS_ROOT(dentry))
		return 1;
	lower = PF_D(dentry)->lower.dentry;
	if (!d_really_is_positive(dentry) || !d_really_is_positive(lower))
		return 0;
	/* A renamed/unlinked backing dentry must not preserve an old pathname. */
	spin_lock(&dentry->d_lock);
	spin_lock_nested(&lower->d_lock, DENTRY_D_LOCK_NESTED);
	valid = !d_unhashed(lower) &&
		lower->d_name.len == dentry->d_name.len &&
		!memcmp(lower->d_name.name, dentry->d_name.name, lower->d_name.len);
	spin_unlock(&lower->d_lock);
	spin_unlock(&dentry->d_lock);
	if (valid && (lower->d_flags & DCACHE_OP_REVALIDATE))
		valid = lower->d_op->d_revalidate(lower, flags);
	return valid;
}

const struct dentry_operations pf_dentry_ops = {
	.d_revalidate = pf_d_revalidate,
	.d_release = pf_d_release,
};

void pf_parent_path(struct dentry *parent, enum pf_backend backend,
		    struct path *result)
{
	struct pf_sb *sbi = PF_SB(parent->d_sb);

	if (IS_ROOT(parent))
		*result = backend == PF_MEMORY ? sbi->memory : sbi->source;
	else
		*result = PF_D(parent)->lower;
	path_get(result);
}

static int pf_lookup_one(struct dentry *parent, const struct qstr *name,
			 enum pf_backend backend, struct path *result)
{
	struct path base;
	struct dentry *lower;

	pf_parent_path(parent, backend, &base);
	lower = lookup_one_len_unlocked(name->name, base.dentry, name->len);
	if (IS_ERR(lower)) {
		path_put(&base);
		return PTR_ERR(lower);
	}
	/* Never silently ignore a mount placed in a backing tree. */
	if (d_mountpoint(lower)) {
		dput(lower);
		path_put(&base);
		return -EXDEV;
	}
	result->mnt = mntget(base.mnt);
	result->dentry = lower;
	path_put(&base);
	return 0;
}

int pf_lookup_backing(struct dentry *parent, const struct qstr *name,
		      struct path *result, enum pf_backend *backend)
{
	struct path source, memory;
	struct inode *inode;
	int error;

	*backend = PF_D(parent)->backend;
	if (!IS_ROOT(parent) || !pf_matches(PF_SB(parent->d_sb), name))
		return pf_lookup_one(parent, name, *backend, result);
	error = pf_lookup_one(parent, name, PF_SOURCE, &source);
	if (error)
		return error;
	inode = d_inode(source.dentry);
	/* Prefix matching applies to directories, never regular files/symlinks. */
	if (inode && !S_ISDIR(inode->i_mode)) {
		*result = source;
		*backend = PF_SOURCE;
		return 0;
	}
	error = pf_lookup_one(parent, name, PF_MEMORY, &memory);
	if (error) {
		path_put(&source);
		return error;
	}
	inode = d_inode(memory.dentry);
	if (inode && !S_ISDIR(inode->i_mode)) {
		path_put(&source);
		path_put(&memory);
		return -EIO;
	}
	if (inode || d_inode(source.dentry)) {
		path_put(&source);
		*result = memory;
		*backend = PF_MEMORY;
	} else {
		path_put(&memory);
		*result = source;
		*backend = PF_SOURCE;
	}
	return 0;
}

static struct dentry *pf_lookup(struct inode *dir, struct dentry *dentry,
				unsigned int flags)
{
	struct path lower;
	enum pf_backend backend;
	struct inode *inode = NULL;
	int error;

	error = pf_lookup_backing(dentry->d_parent, &dentry->d_name, &lower, &backend);
	if (error)
		return ERR_PTR(error);
	error = pf_set_path(dentry, &lower, backend);
	if (!error && d_inode(lower.dentry)) {
		inode = pf_iget(dir->i_sb, d_inode(lower.dentry), backend);
		if (IS_ERR(inode))
			error = PTR_ERR(inode);
	}
	path_put(&lower);
	if (error)
		return ERR_PTR(error);
	return d_splice_alias(inode, dentry);
}

static enum pf_backend pf_target_backend(struct dentry *dentry, bool directory)
{
	struct dentry *parent = dentry->d_parent;

	if (!IS_ROOT(parent))
		return PF_D(parent)->backend;
	return directory && pf_matches(PF_SB(parent->d_sb), &dentry->d_name)
		? PF_MEMORY : PF_SOURCE;
}

static int pf_permission(struct mnt_idmap *idmap, struct inode *inode, int mask)
{
	struct pf_sb *sbi = PF_SB(inode->i_sb);
	struct path *root = PF_I(inode)->backend == PF_MEMORY
		? &sbi->memory : &sbi->source;

	if ((mask & MAY_EXEC) && S_ISREG(inode->i_mode) &&
	    ((root->mnt->mnt_flags & MNT_NOEXEC) ||
	     (root->mnt->mnt_sb->s_iflags & SB_I_NOEXEC)))
		return -EACCES;
	return inode_permission(mnt_idmap(root->mnt), PF_I(inode)->lower, mask);
}

static int pf_getattr(struct mnt_idmap *idmap, const struct path *path,
		      struct kstat *stat, u32 mask, unsigned int flags)
{
	int error = vfs_getattr(&PF_D(path->dentry)->lower, stat, mask, flags);

	if (!error) {
		pf_refresh(d_inode(path->dentry));
		if (S_ISREG(stat->mode))
			stat->mode &= ~(S_ISUID | S_ISGID);
	}
	return error;
}

static int pf_setattr(struct mnt_idmap *idmap, struct dentry *dentry,
		      struct iattr *attributes)
{
	struct path *lower = &PF_D(dentry)->lower;
	struct inode *inode = d_inode(lower->dentry);
	struct iattr attr = *attributes;
	int error;
	bool write_access = false;

	if ((attr.ia_valid & ATTR_MODE) && S_ISREG(inode->i_mode) &&
	    (attr.ia_mode & (S_ISUID | S_ISGID)))
		return -EOPNOTSUPP;
	if (attr.ia_valid & ATTR_FILE)
		attr.ia_file = PF_F(attr.ia_file)->lower;
	error = mnt_want_write(lower->mnt);
	if (error)
		return error;
	if (attr.ia_valid & ATTR_SIZE) {
		error = get_write_access(inode);
		if (error)
			goto out_write;
		write_access = true;
	}
	inode_lock(inode);
	error = notify_change(mnt_idmap(lower->mnt), lower->dentry, &attr, NULL);
	inode_unlock(inode);
	if (write_access)
		put_write_access(inode);
	if (!error) {
		pf_refresh(d_inode(dentry));
		forget_all_cached_acls(d_inode(dentry));
	}
out_write:
	mnt_drop_write(lower->mnt);
	return error;
}

enum pf_create_type { PF_CREATE, PF_MKDIR, PF_MKNOD, PF_SYMLINK };

static int pf_create_common(struct inode *dir, struct dentry *dentry,
			   umode_t mode, dev_t dev, const char *target,
			   enum pf_create_type type, bool exclusive)
{
	enum pf_backend backend = pf_target_backend(dentry, type == PF_MKDIR);
	struct path parent, lower;
	struct inode *inode;
	int error, rollback;

	if (type == PF_CREATE && (mode & (S_ISUID | S_ISGID)))
		return -EOPNOTSUPP;
	pf_parent_path(dentry->d_parent, backend, &parent);
	error = mnt_want_write(parent.mnt);
	if (error)
		goto out_parent;
	inode_lock_nested(d_inode(parent.dentry), I_MUTEX_PARENT);
	lower.mnt = parent.mnt;
	lower.dentry = lookup_one_len(dentry->d_name.name, parent.dentry,
				     dentry->d_name.len);
	if (IS_ERR(lower.dentry)) {
		error = PTR_ERR(lower.dentry);
		goto out_unlock;
	}
	/* Allocate private dentry state before publishing any backing object. */
	error = pf_set_path(dentry, &lower, backend);
	if (error)
		goto out_lower;
	switch (type) {
	case PF_CREATE:
		error = vfs_create(mnt_idmap(parent.mnt), d_inode(parent.dentry),
				   lower.dentry, mode, exclusive);
		break;
	case PF_MKDIR:
		error = vfs_mkdir(mnt_idmap(parent.mnt), d_inode(parent.dentry),
				  lower.dentry, mode);
		break;
	case PF_MKNOD:
		error = vfs_mknod(mnt_idmap(parent.mnt), d_inode(parent.dentry),
				  lower.dentry, mode, dev);
		break;
	case PF_SYMLINK:
		error = vfs_symlink(mnt_idmap(parent.mnt), d_inode(parent.dentry),
				    lower.dentry, target);
		break;
	default:
		error = -EINVAL;
	}
	if (error)
		goto out_lower;
	inode = pf_iget(dir->i_sb, d_inode(lower.dentry), backend);
	if (IS_ERR(inode)) {
		error = PTR_ERR(inode);
		if (type == PF_MKDIR)
			rollback = vfs_rmdir(mnt_idmap(parent.mnt),
					     d_inode(parent.dentry), lower.dentry);
		else
			rollback = vfs_unlink(mnt_idmap(parent.mnt),
					      d_inode(parent.dentry), lower.dentry, NULL);
		if (rollback)
			pr_err_ratelimited("prefixfs: creation rollback failed: %d\n", rollback);
	} else {
		d_instantiate(dentry, inode);
	}
	pf_refresh(dir);
out_lower:
	dput(lower.dentry);
out_unlock:
	inode_unlock(d_inode(parent.dentry));
	mnt_drop_write(parent.mnt);
out_parent:
	path_put(&parent);
	return error;
}

static int pf_create(struct mnt_idmap *idmap, struct inode *dir,
		     struct dentry *dentry, umode_t mode, bool exclusive)
{
	return pf_create_common(dir, dentry, mode, 0, NULL, PF_CREATE, exclusive);
}

static int pf_mkdir(struct mnt_idmap *idmap, struct inode *dir,
		    struct dentry *dentry, umode_t mode)
{
	return pf_create_common(dir, dentry, mode, 0, NULL, PF_MKDIR, false);
}

static int pf_mknod(struct mnt_idmap *idmap, struct inode *dir,
		    struct dentry *dentry, umode_t mode, dev_t dev)
{
	return pf_create_common(dir, dentry, mode, dev, NULL, PF_MKNOD, false);
}

static int pf_symlink(struct mnt_idmap *idmap, struct inode *dir,
		      struct dentry *dentry, const char *target)
{
	return pf_create_common(dir, dentry, S_IFLNK | 0777, 0, target,
				PF_SYMLINK, false);
}

static int pf_remove(struct inode *dir, struct dentry *dentry, bool directory)
{
	struct path *lower = &PF_D(dentry)->lower;
	struct dentry *parent = dget_parent(lower->dentry);
	int error;

	error = mnt_want_write(lower->mnt);
	if (error)
		goto out;
	inode_lock_nested(d_inode(parent), I_MUTEX_PARENT);
	if (directory)
		error = vfs_rmdir(mnt_idmap(lower->mnt), d_inode(parent), lower->dentry);
	else
		error = vfs_unlink(mnt_idmap(lower->mnt), d_inode(parent), lower->dentry, NULL);
	if (!error) {
		pf_refresh(d_inode(dentry));
		pf_refresh(dir);
	}
	inode_unlock(d_inode(parent));
	mnt_drop_write(lower->mnt);
out:
	dput(parent);
	return error;
}

static int pf_unlink(struct inode *dir, struct dentry *dentry)
{
	return pf_remove(dir, dentry, false);
}

static int pf_rmdir(struct inode *dir, struct dentry *dentry)
{
	return pf_remove(dir, dentry, true);
}

static int pf_link(struct dentry *old, struct inode *dir, struct dentry *new)
{
	struct path parent, lower;
	struct path *source = &PF_D(old)->lower;
	enum pf_backend backend = pf_target_backend(new, false);
	int error;

	if (backend != PF_D(old)->backend)
		return -EXDEV;
	pf_parent_path(new->d_parent, backend, &parent);
	if (parent.mnt != source->mnt) {
		error = -EXDEV;
		goto out_parent;
	}
	error = mnt_want_write(parent.mnt);
	if (error)
		goto out_parent;
	inode_lock_nested(d_inode(parent.dentry), I_MUTEX_PARENT);
	lower.mnt = parent.mnt;
	lower.dentry = lookup_one_len(new->d_name.name, parent.dentry, new->d_name.len);
	if (IS_ERR(lower.dentry)) {
		error = PTR_ERR(lower.dentry);
		goto out_unlock;
	}
	error = pf_set_path(new, &lower, backend);
	if (!error)
		error = vfs_link(source->dentry, mnt_idmap(parent.mnt),
				 d_inode(parent.dentry), lower.dentry, NULL);
	if (!error) {
		ihold(d_inode(old));
		d_instantiate(new, d_inode(old));
		pf_refresh(d_inode(old));
		pf_refresh(dir);
	}
	dput(lower.dentry);
out_unlock:
	inode_unlock(d_inode(parent.dentry));
	mnt_drop_write(parent.mnt);
out_parent:
	path_put(&parent);
	return error;
}

static int pf_rename(struct mnt_idmap *idmap, struct inode *old_dir,
		     struct dentry *old, struct inode *new_dir,
		     struct dentry *new, unsigned int flags)
{
	enum pf_backend backend = pf_target_backend(new, d_is_dir(old));
	struct path old_parent, new_parent, target;
	struct dentry *trap;
	struct renamedata data;
	int error;

	if (flags & ~(RENAME_NOREPLACE | RENAME_EXCHANGE))
		return -EOPNOTSUPP;
	if (backend != PF_D(old)->backend ||
	    (d_really_is_positive(new) && backend != PF_D(new)->backend))
		return -EXDEV;
	/* Exchange routes both objects, including the object moving to old. */
	if ((flags & RENAME_EXCHANGE) &&
	    pf_target_backend(old, d_is_dir(new)) != PF_D(new)->backend)
		return -EXDEV;
	pf_parent_path(old->d_parent, backend, &old_parent);
	pf_parent_path(new->d_parent, backend, &new_parent);
	if (old_parent.mnt != new_parent.mnt) {
		error = -EXDEV;
		goto out_parents;
	}
	error = mnt_want_write(old_parent.mnt);
	if (error)
		goto out_parents;
	trap = lock_rename(old_parent.dentry, new_parent.dentry);
	target.mnt = new_parent.mnt;
	target.dentry = lookup_one_len(new->d_name.name, new_parent.dentry,
				      new->d_name.len);
	if (IS_ERR(target.dentry)) {
		error = PTR_ERR(target.dentry);
		goto out_unlock;
	}
	if (trap == PF_D(old)->lower.dentry || trap == target.dentry) {
		error = trap == target.dentry ? -ENOTEMPTY : -EINVAL;
		goto out_target;
	}
	if (!d_really_is_positive(new)) {
		error = pf_set_path(new, &target, backend);
		if (error)
			goto out_target;
	}
	data = (struct renamedata) {
		.old_mnt_idmap = mnt_idmap(old_parent.mnt),
		.old_dir = d_inode(old_parent.dentry),
		.old_dentry = PF_D(old)->lower.dentry,
		.new_mnt_idmap = mnt_idmap(new_parent.mnt),
		.new_dir = d_inode(new_parent.dentry),
		.new_dentry = target.dentry,
		.flags = flags,
	};
	error = vfs_rename(&data);
	if (!error) {
		pf_refresh(old_dir);
		pf_refresh(new_dir);
		pf_refresh(d_inode(old));
		if (d_inode(new))
			pf_refresh(d_inode(new));
	}
out_target:
	dput(target.dentry);
out_unlock:
	unlock_rename(old_parent.dentry, new_parent.dentry);
	mnt_drop_write(old_parent.mnt);
out_parents:
	path_put(&old_parent);
	path_put(&new_parent);
	return error;
}

static const char *pf_get_link(struct dentry *dentry, struct inode *inode,
			       struct delayed_call *done)
{
	if (!dentry)
		return ERR_PTR(-ECHILD);
	return vfs_get_link(PF_D(dentry)->lower.dentry, done);
}

static ssize_t pf_listxattr(struct dentry *dentry, char *buffer, size_t size)
{
	return vfs_listxattr(PF_D(dentry)->lower.dentry, buffer, size);
}

static int pf_xattr_get(const struct xattr_handler *handler, struct dentry *dentry,
			struct inode *inode, const char *name, void *buffer, size_t size)
{
	struct path *lower = &PF_D(dentry)->lower;

	if (!strcmp(name, XATTR_NAME_CAPS))
		return -ENODATA;
	return vfs_getxattr(mnt_idmap(lower->mnt), lower->dentry, name, buffer, size);
}

static int pf_xattr_set(const struct xattr_handler *handler, struct mnt_idmap *idmap,
			struct dentry *dentry, struct inode *inode, const char *name,
			const void *value, size_t size, int flags)
{
	struct path *lower = &PF_D(dentry)->lower;
	int error;

	if (!strcmp(name, XATTR_NAME_CAPS))
		return -EOPNOTSUPP;
	error = mnt_want_write(lower->mnt);
	if (error)
		return error;
	if (value)
		error = vfs_setxattr(mnt_idmap(lower->mnt), lower->dentry, name, value, size, flags);
	else
		error = vfs_removexattr(mnt_idmap(lower->mnt), lower->dentry, name);
	mnt_drop_write(lower->mnt);
	if (!error)
		pf_refresh(inode);
	return error;
}

static const struct xattr_handler pf_xattr_handler = {
	.prefix = "",
	.get = pf_xattr_get,
	.set = pf_xattr_set,
};
const struct xattr_handler * const pf_xattr_handlers[] = {
	&pf_xattr_handler,
	NULL,
};

static const char *pf_acl_name(int type)
{
	return type == ACL_TYPE_ACCESS ? XATTR_NAME_POSIX_ACL_ACCESS : XATTR_NAME_POSIX_ACL_DEFAULT;
}

static struct posix_acl *pf_get_inode_acl(struct inode *inode, int type, bool rcu)
{
	if (rcu)
		return ERR_PTR(-ECHILD);
	return get_inode_acl(PF_I(inode)->lower, type);
}

static struct posix_acl *pf_get_acl(struct mnt_idmap *idmap,
				   struct dentry *dentry, int type)
{
	struct path *lower = &PF_D(dentry)->lower;

	return vfs_get_acl(mnt_idmap(lower->mnt), lower->dentry, pf_acl_name(type));
}

static int pf_set_acl(struct mnt_idmap *idmap, struct dentry *dentry,
		      struct posix_acl *acl, int type)
{
	struct path *lower = &PF_D(dentry)->lower;
	int error = mnt_want_write(lower->mnt);

	if (error)
		return error;
	if (acl)
		error = vfs_set_acl(mnt_idmap(lower->mnt), lower->dentry, pf_acl_name(type), acl);
	else
		error = vfs_remove_acl(mnt_idmap(lower->mnt), lower->dentry, pf_acl_name(type));
	mnt_drop_write(lower->mnt);
	if (!error) {
		pf_refresh(d_inode(dentry));
		forget_all_cached_acls(d_inode(dentry));
	}
	return error;
}

#define PF_METADATA_OPS \
	.permission = pf_permission, \
	.getattr = pf_getattr, \
	.setattr = pf_setattr, \
	.listxattr = pf_listxattr, \
	.get_inode_acl = pf_get_inode_acl, \
	.get_acl = pf_get_acl, \
	.set_acl = pf_set_acl

const struct inode_operations pf_dir_iops = {
	PF_METADATA_OPS,
	.lookup = pf_lookup,
	.create = pf_create,
	.mkdir = pf_mkdir,
	.mknod = pf_mknod,
	.symlink = pf_symlink,
	.link = pf_link,
	.unlink = pf_unlink,
	.rmdir = pf_rmdir,
	.rename = pf_rename,
};
const struct inode_operations pf_file_iops = {
	PF_METADATA_OPS,
};
const struct inode_operations pf_symlink_iops = {
	PF_METADATA_OPS,
	.get_link = pf_get_link,
};

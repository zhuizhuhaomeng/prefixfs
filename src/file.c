// SPDX-License-Identifier: GPL-2.0-only
#include <linux/file.h>
#ifdef PF_FILELOCK_HEADER
#include <linux/filelock.h>
#endif
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/poll.h>
#include <linux/sched/mm.h>
#include <linux/splice.h>
#include <linux/uio.h>
#include "prefixfs.h"

int pf_file_open(struct inode *inode, struct file *file)
{
	struct pf_file *state = kzalloc(sizeof(*state), GFP_KERNEL);
	int error;
	fmode_t flags = FMODE_LSEEK | FMODE_PREAD | FMODE_PWRITE | FMODE_NOWAIT;

	if (!state)
		return -ENOMEM;
	state->lower = dentry_open(&PF_D(file->f_path.dentry)->lower,
				   file->f_flags, file->f_cred);
	if (IS_ERR(state->lower)) {
		error = PTR_ERR(state->lower);
		goto out_state;
	}
	if (S_ISDIR(inode->i_mode) && IS_ROOT(file->f_path.dentry)) {
		state->memory = dentry_open(&PF_SB(inode->i_sb)->memory,
					    O_RDONLY | O_DIRECTORY, file->f_cred);
		if (IS_ERR(state->memory)) {
			error = PTR_ERR(state->memory);
			goto out_lower;
		}
	}
	file->private_data = state;
	file->f_mapping = state->lower->f_mapping;
	file->f_mode = (file->f_mode & ~flags) | (state->lower->f_mode & flags);
	pf_refresh(inode);
	return 0;
out_lower:
	fput(state->lower);
out_state:
	kfree(state);
	return error;
}

int pf_file_release(struct inode *inode, struct file *file)
{
	struct pf_file *state = PF_F(file);

	pf_snapshot_free(state->snapshot);
	if (state->memory)
		fput(state->memory);
	fput(state->lower);
	kfree(state);
	return 0;
}

static loff_t pf_llseek(struct file *file, loff_t offset, int whence)
{
	struct file *lower = PF_F(file)->lower;
	loff_t result;

	lower->f_pos = file->f_pos;
	result = vfs_llseek(lower, offset, whence);
	if (result >= 0)
		file->f_pos = result;
	return result;
}

static ssize_t pf_read_iter(struct kiocb *iocb, struct iov_iter *iter)
{
	struct file *file = iocb->ki_filp;
	ssize_t result = vfs_iter_read(PF_F(file)->lower, iter, &iocb->ki_pos,
				       (__force rwf_t)(iocb->ki_flags & RWF_SUPPORTED));

	if (result >= 0)
		pf_refresh(file_inode(file));
	return result;
}

static ssize_t pf_write_iter(struct kiocb *iocb, struct iov_iter *iter)
{
	struct file *file = iocb->ki_filp;
	ssize_t result = vfs_iter_write(PF_F(file)->lower, iter, &iocb->ki_pos,
					(__force rwf_t)(iocb->ki_flags & RWF_SUPPORTED));

	if (result >= 0)
		pf_refresh(file_inode(file));
	return result;
}

static int pf_fsync(struct file *file, loff_t start, loff_t end, int datasync)
{
	return vfs_fsync_range(PF_F(file)->lower, start, end, datasync);
}

static long pf_fallocate(struct file *file, int mode, loff_t offset, loff_t len)
{
	long result = vfs_fallocate(PF_F(file)->lower, mode, offset, len);

	if (!result)
		pf_refresh(file_inode(file));
	return result;
}

static int pf_flush(struct file *file, fl_owner_t owner)
{
	struct file *lower = PF_F(file)->lower;

	locks_remove_posix(lower, owner);
	return lower->f_op->flush ? lower->f_op->flush(lower, owner) : 0;
}

static int pf_lock(struct file *file, int cmd, struct file_lock *lock)
{
	struct file *lower = PF_F(file)->lower;

	lock->fl_file = lower;
	if (cmd == F_GETLK)
		return vfs_test_lock(lower, lock);
	return vfs_lock_file(lower, cmd, lock, NULL);
}

static int pf_flock(struct file *file, int cmd, struct file_lock *lock)
{
	struct file *lower = PF_F(file)->lower;

	lock->fl_file = lower;
	if (lower->f_op->flock)
		return lower->f_op->flock(lower, cmd, lock);
	return locks_lock_file_wait(lower, lock);
}

static __poll_t pf_poll(struct file *file, poll_table *wait)
{
	struct file *lower = PF_F(file)->lower;

	return lower->f_op->poll ? lower->f_op->poll(lower, wait) : DEFAULT_POLLMASK;
}

static long pf_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct file *lower = PF_F(file)->lower;

	return lower->f_op->unlocked_ioctl
		? lower->f_op->unlocked_ioctl(lower, cmd, arg) : -ENOTTY;
}

#ifdef CONFIG_COMPAT
static long pf_compat_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct file *lower = PF_F(file)->lower;

	return lower->f_op->compat_ioctl
		? lower->f_op->compat_ioctl(lower, cmd, arg) : -ENOIOCTLCMD;
}
#endif

static int pf_mmap(struct file *file, struct vm_area_struct *vma)
{
	struct file *lower = PF_F(file)->lower;

	if (!lower->f_op->mmap)
		return -ENODEV;
	if ((lower->f_path.mnt->mnt_flags & MNT_NOEXEC) ||
	    (file_inode(lower)->i_sb->s_iflags & SB_I_NOEXEC)) {
		if (vma->vm_flags & VM_EXEC)
			return -EPERM;
		vm_flags_clear(vma, VM_MAYEXEC);
	}
	/* The VMA pins the real file; all page faults use its native mapping. */
	vma_set_file(vma, lower);
	return call_mmap(lower, vma);
}

static unsigned long pf_get_unmapped_area(struct file *file, unsigned long addr,
					 unsigned long len, unsigned long pgoff,
					 unsigned long flags)
{
	struct file *lower = PF_F(file)->lower;

	if (lower->f_op->get_unmapped_area)
		return lower->f_op->get_unmapped_area(lower, addr, len, pgoff, flags);
#ifdef PF_MM_UNMAPPED_AREA
	return mm_get_unmapped_area(current->mm, lower, addr, len, pgoff, flags);
#else
	return current->mm->get_unmapped_area(lower, addr, len, pgoff, flags);
#endif
}

static ssize_t pf_copy_file_range(struct file *in, loff_t in_pos,
				  struct file *out, loff_t out_pos,
				  size_t len, unsigned int flags)
{
	ssize_t result;

	if (out->f_op != &pf_file_ops ||
	    PF_D(in->f_path.dentry)->backend != PF_D(out->f_path.dentry)->backend)
		return -EXDEV;
	result = vfs_copy_file_range(PF_F(in)->lower, in_pos,
				    PF_F(out)->lower, out_pos, len, flags);
	if (result >= 0)
		pf_refresh(file_inode(out));
	return result;
}

const struct file_operations pf_file_ops = {
	.owner = THIS_MODULE,
	.open = pf_file_open,
	.release = pf_file_release,
	.llseek = pf_llseek,
	.read_iter = pf_read_iter,
	.write_iter = pf_write_iter,
	.fsync = pf_fsync,
	.fallocate = pf_fallocate,
	.flush = pf_flush,
	.lock = pf_lock,
	.flock = pf_flock,
	.poll = pf_poll,
	.unlocked_ioctl = pf_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = pf_compat_ioctl,
#endif
	.mmap = pf_mmap,
	.get_unmapped_area = pf_get_unmapped_area,
	.splice_read = generic_file_splice_read,
	.splice_write = iter_file_splice_write,
	.copy_file_range = pf_copy_file_range,
};

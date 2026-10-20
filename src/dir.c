// SPDX-License-Identifier: GPL-2.0-only
#include <linux/module.h>
#include <linux/namei.h>
#include "prefixfs.h"

#ifdef PF_BOOL_ACTOR
typedef bool pf_actor_result;
#define PF_CONTINUE true
#define PF_STOP false
#else
typedef int pf_actor_result;
#define PF_CONTINUE 0
#define PF_STOP (-ENOMEM)
#endif

struct pf_collect {
	struct dir_context ctx;
	struct pf_snapshot *snapshot;
	struct pf_sb *sbi;
	bool memory;
	int error;
};

static pf_actor_result pf_collect_name(struct dir_context *context,
				       const char *name, int len, loff_t offset,
				       u64 ino, unsigned int type)
{
	struct pf_collect *collect = container_of(context, struct pf_collect, ctx);
	struct pf_snapshot *snapshot = collect->snapshot;
	struct rb_node **link = &snapshot->names.rb_node, *parent = NULL;
	struct pf_dirent *entry;
	struct qstr qname = QSTR_INIT(name, len);
	bool matching = pf_matches(collect->sbi, &qname);
	int compare;

	if ((len == 1 && name[0] == '.') ||
	    (len == 2 && name[0] == '.' && name[1] == '.'))
		return PF_CONTINUE;
	if (collect->memory && (!matching || (type != DT_DIR && type != DT_UNKNOWN)))
		return PF_CONTINUE;
	if (!collect->memory && matching && type == DT_DIR)
		return PF_CONTINUE;
	while (*link) {
		parent = *link;
		entry = rb_entry(parent, struct pf_dirent, node);
		compare = memcmp(name, entry->name, min_t(unsigned int, len, entry->len));
		if (!compare)
			compare = len - (int)entry->len;
		if (compare < 0)
			link = &parent->rb_left;
		else if (compare > 0)
			link = &parent->rb_right;
		else {
			/* A source file wins over an identically named memory directory. */
			if (entry->type == DT_UNKNOWN)
				entry->needs_lookup = true;
			return PF_CONTINUE;
		}
	}
	entry = kmalloc(sizeof(*entry) + len + 1, GFP_NOFS);
	if (!entry) {
		collect->error = -ENOMEM;
		return PF_STOP;
	}
	entry->ino = ino;
	entry->type = type;
	entry->len = len;
	entry->needs_lookup = matching && type == DT_UNKNOWN;
	memcpy(entry->name, name, len);
	entry->name[len] = '\0';
	rb_link_node(&entry->node, parent, link);
	rb_insert_color(&entry->node, &snapshot->names);
	snapshot->count++;
	return PF_CONTINUE;
}

void pf_snapshot_free(struct pf_snapshot *snapshot)
{
	struct rb_node *node;

	if (!snapshot)
		return;
	while ((node = rb_first(&snapshot->names))) {
		rb_erase(node, &snapshot->names);
		kfree(rb_entry(node, struct pf_dirent, node));
	}
	kvfree(snapshot->entries);
	kfree(snapshot);
}

static int pf_build_snapshot(struct file *file)
{
	struct pf_file *state = PF_F(file);
	struct pf_snapshot *snapshot;
	struct pf_collect collect;
	struct rb_node *node, *next;
	unsigned int index = 0;
	int error;

	snapshot = kzalloc(sizeof(*snapshot), GFP_NOFS);
	if (!snapshot)
		return -ENOMEM;
	snapshot->names = RB_ROOT;
	collect = (struct pf_collect) {
		.ctx.actor = pf_collect_name,
		.snapshot = snapshot,
		.sbi = PF_SB(file_inode(file)->i_sb),
	};
	error = vfs_llseek(state->lower, 0, SEEK_SET);
	if (error < 0)
		goto out;
	error = iterate_dir(state->lower, &collect.ctx);
	if (error || collect.error) {
		error = error ? error : collect.error;
		goto out;
	}
	collect.memory = true;
	collect.ctx.pos = 0;
	error = vfs_llseek(state->memory, 0, SEEK_SET);
	if (error < 0)
		goto out;
	error = iterate_dir(state->memory, &collect.ctx);
	if (error || collect.error) {
		error = error ? error : collect.error;
		goto out;
	}
	/* Local filesystems normally supply d_type: only ambiguous rows need lookup. */
	for (node = rb_first(&snapshot->names); node; node = next) {
		struct pf_dirent *entry = rb_entry(node, struct pf_dirent, node);
		struct qstr name = QSTR_INIT(entry->name, entry->len);
		struct path lower;
		enum pf_backend backend;

		next = rb_next(node);
		if (!entry->needs_lookup)
			continue;
		error = pf_lookup_backing(file->f_path.dentry, &name, &lower, &backend);
		if (error)
			goto out;
		if (!d_inode(lower.dentry)) {
			rb_erase(node, &snapshot->names);
			kfree(entry);
			snapshot->count--;
		} else {
			entry->ino = d_inode(lower.dentry)->i_ino;
			entry->type = (d_inode(lower.dentry)->i_mode >> 12) & 15;
		}
		path_put(&lower);
	}
	snapshot->entries = kvmalloc_array(snapshot->count,
					 sizeof(*snapshot->entries), GFP_NOFS);
	if (!snapshot->entries && snapshot->count) {
		error = -ENOMEM;
		goto out;
	}
	for (node = rb_first(&snapshot->names); node; node = rb_next(node))
		snapshot->entries[index++] = rb_entry(node, struct pf_dirent, node);
	state->snapshot = snapshot;
	return 0;
out:
	pf_snapshot_free(snapshot);
	return error;
}

int pf_root_iterate(struct file *file, struct dir_context *ctx)
{
	struct pf_snapshot *snapshot;
	int error;

	if (!dir_emit_dots(file, ctx))
		return 0;
	if (!PF_F(file)->snapshot) {
		error = pf_build_snapshot(file);
		if (error)
			return error;
	}
	snapshot = PF_F(file)->snapshot;
	while (ctx->pos >= 2 && ctx->pos - 2 < snapshot->count) {
		struct pf_dirent *entry = snapshot->entries[ctx->pos - 2];

		if (!dir_emit(ctx, entry->name, entry->len, entry->ino, entry->type))
			return 0;
		ctx->pos++;
	}
	return 0;
}

static int pf_iterate(struct file *file, struct dir_context *ctx)
{
	struct file *lower = PF_F(file)->lower;

	if (IS_ROOT(file->f_path.dentry))
		return pf_root_iterate(file, ctx);
	lower->f_pos = file->f_pos;
	return iterate_dir(lower, ctx);
}

static loff_t pf_dir_llseek(struct file *file, loff_t offset, int whence)
{
	struct file *lower = PF_F(file)->lower;
	loff_t result;

	if (!IS_ROOT(file->f_path.dentry)) {
		lower->f_pos = file->f_pos;
		result = vfs_llseek(lower, offset, whence);
		if (result >= 0)
			file->f_pos = result;
		return result;
	}
	result = generic_file_llseek(file, offset, whence);
	if (!result) {
		pf_snapshot_free(PF_F(file)->snapshot);
		PF_F(file)->snapshot = NULL;
	}
	return result;
}

static int pf_dir_fsync(struct file *file, loff_t start, loff_t end, int datasync)
{
	int error = vfs_fsync_range(PF_F(file)->lower, start, end, datasync);
	int other;

	if (!PF_F(file)->memory)
		return error;
	other = vfs_fsync_range(PF_F(file)->memory, start, end, datasync);
	return error ? error : other;
}

const struct file_operations pf_dir_ops = {
	.owner = THIS_MODULE,
	.open = pf_file_open,
	.release = pf_file_release,
	.read = generic_read_dir,
	.iterate_shared = pf_iterate,
	.llseek = pf_dir_llseek,
	.fsync = pf_dir_fsync,
};

obj-m += prefixfs.o
prefixfs-y := src/super.o src/inode.o src/file.o src/dir.o
ccflags-y += -I$(src)/include
ifeq ($(shell grep -c 'struct mnt_idmap \*mnt_idmap' $(srctree)/include/linux/mount.h),0)
$(error prefixfs requires the mnt_idmap VFS API; use matching supported kernel headers)
endif
ifneq ($(shell grep -c 'typedef bool (\*filldir_t)' $(srctree)/include/linux/fs.h),0)
ccflags-y += -DPF_BOOL_ACTOR
endif

ifneq ($(wildcard $(srctree)/include/linux/filelock.h),)
ccflags-y += -DPF_FILELOCK_HEADER
endif
ifneq ($(shell grep -c '^unsigned long mm_get_unmapped_area' $(srctree)/include/linux/sched/mm.h),0)
ccflags-y += -DPF_MM_UNMAPPED_AREA
endif

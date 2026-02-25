/* SPDX-License-Identifier: GPL-2.0 */
/*
 * daxfs GPU mapping helper - public API
 *
 * Maps a daxfs DAX region into GPU address space so that CUDA kernels
 * can issue PCIe AtomicOps (CAS TLPs) against the coordination lock
 * and shared page cache.
 *
 * Primary path : dma-buf fd  -> cuImportExternalMemory (OPAQUE_FD)
 * Fallback path: /dev/mem    -> cuMemHostRegister (phys= mounts)
 *
 * Copyright (C) 2026 Multikernel Technologies, Inc. All rights reserved.
 */
#ifndef _DAXFS_GPU_MAP_H
#define _DAXFS_GPU_MAP_H

#include <cuda.h>
#include <stdint.h>
#include "daxfs_format.h"

struct daxfs_gpu_map {
	/* GPU mapping */
	CUdeviceptr			base;		/* GPU pointer to DAX base */
	CUexternalMemory		ext_mem;	/* Handle (dma-buf path) */
	size_t				size;		/* Total mapped size */

	/* Layout from ioctl */
	struct daxfs_gpu_info		info;

	/* Convenience device pointers (computed from base + offsets) */
	CUdeviceptr			coord_lock;	/* -> coord_lock field */
	CUdeviceptr			commit_seq;	/* -> commit_sequence */
	CUdeviceptr			pcache_slots;	/* -> slot metadata */
	CUdeviceptr			pcache_data;	/* -> slot data area */
	CUdeviceptr			pending_count;	/* -> pending_count */

	/* Internal state */
	int				mount_fd;	/* fd used for ioctl */
	int				dmabuf_fd;	/* dma-buf fd (-1 if none) */
	void				*host_mmap;	/* mmap addr (/dev/mem path) */
};

/*
 * Map the daxfs DAX region into GPU address space.
 *
 * @mount_fd: open fd on any file within the daxfs mount
 * @gpu:      output structure (zeroed by caller)
 *
 * Returns 0 on success, -1 on failure (errno set).
 * On success the caller must eventually call daxfs_gpu_unmap().
 */
int daxfs_gpu_map(int mount_fd, struct daxfs_gpu_map *gpu);

/*
 * Unmap and release all GPU and host resources.
 */
void daxfs_gpu_unmap(struct daxfs_gpu_map *gpu);

/*
 * Convenience: return a device pointer to a specific pcache slot's
 * state_tag field.
 */
static inline CUdeviceptr
daxfs_gpu_slot_state_tag(const struct daxfs_gpu_map *gpu, uint32_t slot_idx)
{
	return gpu->pcache_slots +
	       (CUdeviceptr)slot_idx * gpu->info.pcache_slot_stride +
	       gpu->info.state_tag_off;
}

/*
 * Convenience: return a device pointer to a specific pcache slot's
 * data page.
 */
static inline CUdeviceptr
daxfs_gpu_slot_data(const struct daxfs_gpu_map *gpu, uint32_t slot_idx)
{
	return gpu->pcache_data + (CUdeviceptr)slot_idx * 4096;
}

#endif /* _DAXFS_GPU_MAP_H */

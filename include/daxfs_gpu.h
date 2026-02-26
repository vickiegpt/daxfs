/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * daxfs GPU-side coordination header (CUDA)
 *
 * Device-inline functions that mirror the kernel's cmpxchg-based
 * coordination protocols for the coordination lock and shared page
 * cache.  Each function compiles to PCIe AtomicOp TLPs (CAS / Swap)
 * that serialize at the memory controller alongside CPU LOCK CMPXCHG,
 * giving mutual atomicity across CPU and GPU.
 *
 * All pointers below are CUdeviceptr-derived device pointers into the
 * DAX region mapped via daxfs-gpu-map.  The caller is responsible for
 * computing field addresses from the base pointer and the offsets
 * returned by DAXFS_IOC_GET_GPU_INFO.
 *
 * Copyright (C) 2026 Multikernel Technologies, Inc. All rights reserved.
 */
#ifndef _DAXFS_GPU_H
#define _DAXFS_GPU_H

#ifdef __CUDACC__

/* Re-export the state/tag helpers so GPU code matches kernel conventions.
 * Guarded so this header can coexist with daxfs_format.h. */
#ifndef PCACHE_STATE_FREE
#define PCACHE_STATE_FREE    0
#define PCACHE_STATE_PENDING 1
#define PCACHE_STATE_VALID   2
#endif

#ifndef PCACHE_STATE
#define PCACHE_STATE(v)        ((v) & 3ULL)
#define PCACHE_TAG(v)          ((v) >> 2)
#define PCACHE_MAKE(state, tag) (((unsigned long long)(tag) << 2) | (state))
#endif

/*
 * =========================================================================
 * Coordination lock (mirrors branch.c daxfs_coord_lock / unlock)
 * =========================================================================
 */

/*
 * Acquire the global coordination lock.
 *
 * @lock: device pointer to coord_lock (__le32, 0 = free, 1 = held)
 *
 * Spins with atomicCAS (32-bit PCIe CAS TLP) until the lock is acquired.
 * __threadfence_system() after acquisition ensures all subsequent GPU
 * loads/stores are ordered after the lock is visible on the PCIe fabric.
 */
static __device__ __forceinline__ void
daxfs_gpu_coord_lock(unsigned int *lock)
{
	while (atomicCAS(lock, 0u, 1u) != 0u)
		;  /* spin */
	__threadfence_system();
}

/*
 * Release the global coordination lock.
 *
 * @lock: device pointer to coord_lock
 *
 * __threadfence_system() before release ensures all prior GPU stores are
 * visible on the PCIe fabric before the lock word is cleared.
 * atomicExch generates a PCIe Swap TLP (unconditional write with return).
 */
static __device__ __forceinline__ void
daxfs_gpu_coord_unlock(unsigned int *lock)
{
	__threadfence_system();
	atomicExch(lock, 0u);
}

/*
 * =========================================================================
 * Commit sequence (mirrors branch.c daxfs_commit_seq_changed)
 * =========================================================================
 */

/*
 * Read the current commit sequence number.
 *
 * @seq: device pointer to commit_sequence (__le64)
 *
 * Returns the current value.  Caller compares against a cached copy to
 * detect new commits.  Uses a volatile load (no atomicCAS needed for
 * read-only access on a naturally-aligned 64-bit word).
 */
static __device__ __forceinline__ unsigned long long
daxfs_gpu_read_commit_seq(const unsigned long long *seq)
{
	return *(volatile const unsigned long long *)seq;
}

/*
 * =========================================================================
 * Page cache slot state machine (mirrors pcache.c slot_cmpxchg)
 * =========================================================================
 */

/*
 * Atomic compare-and-swap on a pcache slot's state_tag field.
 *
 * @state_tag: device pointer to slot->state_tag (__le64)
 * @expected:  expected old value (packed state | tag)
 * @desired:   new value to write if *state_tag == expected
 *
 * Returns the value that was in *state_tag before the operation.
 * Maps to a 64-bit PCIe CAS TLP.
 */
static __device__ __forceinline__ unsigned long long
daxfs_gpu_slot_cmpxchg(unsigned long long *state_tag,
		       unsigned long long expected,
		       unsigned long long desired)
{
	unsigned long long old = atomicCAS(state_tag, expected, desired);

	__threadfence_system();
	return old;
}

/*
 * =========================================================================
 * Pending counter (mirrors pcache.c pcache_inc/dec_pending)
 * =========================================================================
 */

/*
 * Atomically increment the pending slot counter.
 *
 * @pending_count: device pointer to pcache_header->pending_count (__le32)
 *
 * CAS loop identical to the kernel's pcache_inc_pending().
 */
static __device__ __forceinline__ void
daxfs_gpu_pcache_inc_pending(unsigned int *pending_count)
{
	unsigned int old_val, new_val;

	do {
		old_val = *(volatile unsigned int *)pending_count;
		new_val = old_val + 1;
	} while (atomicCAS(pending_count, old_val, new_val) != old_val);
	__threadfence_system();
}

/*
 * Atomically decrement the pending slot counter (saturates at 0).
 *
 * @pending_count: device pointer to pcache_header->pending_count (__le32)
 */
static __device__ __forceinline__ void
daxfs_gpu_pcache_dec_pending(unsigned int *pending_count)
{
	unsigned int old_val, new_val;

	do {
		old_val = *(volatile unsigned int *)pending_count;
		if (old_val == 0)
			return;
		new_val = old_val - 1;
	} while (atomicCAS(pending_count, old_val, new_val) != old_val);
	__threadfence_system();
}

/*
 * =========================================================================
 * Page cache fast-path lookup (mirrors pcache.c daxfs_pcache_get_page)
 * =========================================================================
 */

/*
 * Fast-path cache lookup: load state_tag, check VALID + tag match.
 *
 * @state_tag:   device pointer to slot->state_tag
 * @desired_tag: expected tag value (backing_page_offset >> 12)
 *
 * Returns true if slot is VALID with matching tag (cache hit).
 * Caller can then read directly from the slot data area.
 */
static __device__ __forceinline__ bool
daxfs_gpu_pcache_lookup(const unsigned long long *state_tag,
			unsigned long long desired_tag)
{
	unsigned long long val;

	val = *(volatile const unsigned long long *)state_tag;
	if (val == PCACHE_MAKE(PCACHE_STATE_VALID, desired_tag)) {
		__threadfence_system();  /* order data read after state check */
		return true;
	}
	return false;
}

/*
 * =========================================================================
 * Page cache slot claim (mirrors pcache.c slow path FREE -> PENDING)
 * =========================================================================
 */

/*
 * Attempt to claim a FREE cache slot by transitioning to PENDING.
 *
 * @state_tag:   device pointer to slot->state_tag
 * @desired_tag: tag to install (backing_page_offset >> 12)
 *
 * Returns true if the slot was successfully claimed (FREE -> PENDING).
 * On success the caller must also call daxfs_gpu_pcache_inc_pending().
 * On failure the slot was not FREE or was raced; caller should re-read
 * and retry.
 */
static __device__ __forceinline__ bool
daxfs_gpu_pcache_claim(unsigned long long *state_tag,
		       unsigned long long desired_tag)
{
	unsigned long long free_val = PCACHE_MAKE(PCACHE_STATE_FREE, 0);
	unsigned long long pend_val = PCACHE_MAKE(PCACHE_STATE_PENDING,
						  desired_tag);
	unsigned long long old;

	old = atomicCAS(state_tag, free_val, pend_val);
	__threadfence_system();
	return old == free_val;
}

/*
 * =========================================================================
 * Wait for slot to become VALID (mirrors pcache.c wait_valid)
 * =========================================================================
 */

/*
 * Poll until a PENDING slot transitions to VALID with matching tag.
 *
 * @state_tag:   device pointer to slot->state_tag
 * @desired_tag: expected tag value
 * @max_iters:   maximum poll iterations before giving up
 *
 * Returns true if slot became VALID with matching tag within the
 * iteration budget.  Returns false on timeout or unexpected state
 * (e.g., slot was evicted to FREE).
 */
static __device__ __forceinline__ bool
daxfs_gpu_pcache_wait_valid(const unsigned long long *state_tag,
			    unsigned long long desired_tag,
			    unsigned int max_iters)
{
	unsigned long long val;
	unsigned long long expected = PCACHE_MAKE(PCACHE_STATE_VALID,
						  desired_tag);
	unsigned int i;

	for (i = 0; i < max_iters; i++) {
		val = *(volatile const unsigned long long *)state_tag;
		if (val == expected) {
			__threadfence_system();
			return true;
		}
		/* Slot evicted from under us */
		if (PCACHE_STATE(val) == PCACHE_STATE_FREE)
			return false;
	}
	return false;
}

#endif /* __CUDACC__ */
#endif /* _DAXFS_GPU_H */

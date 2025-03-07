import functools
import typing as tp

import jax
from eformer.escale import with_sharding_constraint
from jax import Array
from jax import numpy as jnp
from jax import random as jr
from jax.experimental.pallas.ops.tpu.splash_attention import (
	BlockSizes,
	CausalMask,
	MultiHeadMask,
	SegmentIds,
	make_splash_mqa_single_device,
)
from jax.experimental.shard_map import shard_map
from jax.sharding import PartitionSpec as Ps

from ._attention_impl import (
	AttentionImpl,
	AttentionMetadata,
	AttentionOutput,
)
from .vanilla import VanillaAttn


class SplashAttn(AttentionImpl):
	def get_impl_name(self) -> str:
		return "splash"

	def get_impl_metadata(self) -> AttentionMetadata:
		return self.metadata

	def forward_native(self, *args, **kwargs) -> AttentionOutput:
		raise NotImplementedError("`forward_native` not implemented!")

	def forward_gpu(self, *args, **kwargs) -> AttentionOutput:
		raise NotImplementedError("`forward_gpu` not implemented!")

	def forward_tpu(
		self,
		q: Array,
		k: Array,
		v: Array,
		mask: tp.Optional[Array] = None,
	) -> AttentionOutput:
		sm_scale = self.metadata.softmax_scale
		sm_scale = sm_scale if sm_scale is not None else q.shape[-1] ** -0.5
		dtype = self.metadata.runtime_dtype
		runtime_type = self.get_runtime_type(q=q, BTHD=False)

		(
			query_partition_spec,
			key_partition_spec,
			value_partition_spec,
			bias_partition_spec,
			mask_partition_spec,
			attention_partition_spec,
		) = self.metadata.get_partition_specs(runtime_type, BTHD=False)
		if mask is not None and mask.shape[0] != q.shape[0]:
			num_reps_mask = q.shape[0] // mask.shape[0]
			mask = jnp.repeat(mask, num_reps_mask, 0)

		query_lenght = q.shape[1]
		value_lenght = v.shape[1]
		block_sizes = BlockSizes(
			block_q=min(self.metadata.blocksize_q, query_lenght),
			block_kv_compute=min(self.metadata.blocksize_k, value_lenght),
			block_kv=min(self.metadata.blocksize_k, value_lenght),
			block_q_dkv=min(self.metadata.blocksize_q, query_lenght),
			block_kv_dkv=min(self.metadata.blocksize_k, value_lenght),
			block_kv_dkv_compute=min(self.metadata.blocksize_k, value_lenght),
			block_q_dq=min(self.metadata.blocksize_q, query_lenght),
			block_kv_dq=min(self.metadata.blocksize_k, value_lenght),
		)
		qkv_mask_partition_spec = Ps(query_partition_spec[0], query_partition_spec[2])
		q_mask, kv_mask = [None] * 2
		if mask is not None:
			q_mask, kv_mask = self._split_attention_mask(mask)
			q_mask, kv_mask = q_mask.astype("i4"), kv_mask.astype("i4")
		pi = [0, 2, 3]

		@functools.partial(
			shard_map,
			mesh=self.metadata.mesh,
			in_specs=(
				self.create_stable_sharding(query_partition_spec, pi, dep=q),
				self.create_stable_sharding(key_partition_spec, pi, dep=k),
				self.create_stable_sharding(value_partition_spec, pi, dep=v),
				self.create_stable_sharding(qkv_mask_partition_spec, [0], dep=q_mask),
				self.create_stable_sharding(qkv_mask_partition_spec, [0], dep=kv_mask),
			),
			out_specs=self.create_stable_sharding(attention_partition_spec, pi),
			check_rep=False,
		)
		def _wraped_flash_attn(q, k, v, q_mask, kv_mask):
			output_shape = q.shape[:-1] + (v.shape[-1],)
			num_reps = q.shape[1] // k.shape[1]
			q = q.reshape(q.shape[:-3] + (k.shape[-3], num_reps, q.shape[-2], q.shape[-1]))
			fn = jax.vmap(
				jax.vmap(
					make_splash_mqa_single_device(
						mask=MultiHeadMask(
							[CausalMask((q.shape[-2], k.shape[-2])) for _ in range(q.shape[-3])]
						),
						block_sizes=block_sizes,
					),
					in_axes=(0, 0, 0, None),
				),
				in_axes=(0, 0, 0, 0),
			)
			m = None
			if kv_mask is not None:
				m = SegmentIds(q_mask, kv_mask)
			return fn(q * sm_scale, k, v, m).reshape(output_shape)

		attn = _wraped_flash_attn(
			q.transpose(0, 2, 1, 3).astype(dtype),
			k.transpose(0, 2, 1, 3).astype(dtype),
			v.transpose(0, 2, 1, 3).astype(dtype),
			q_mask,
			kv_mask,
		).transpose(0, 2, 1, 3)

		return AttentionOutput(
			attention_weights=None,
			attention_outputs=with_sharding_constraint(
				arr=attn,
				sharding=attention_partition_spec,
			),
		)

	def forward_cpu(self, *args, **kwargs) -> AttentionOutput:
		raise NotImplementedError("`forward_cpu` not implemented!")

	def forward_cuda(self, *args, **kwargs) -> AttentionOutput:
		raise NotImplementedError("`forward_cuda` not implemented!")

	def forward_rocm(self, *args, **kwargs) -> AttentionOutput:
		raise NotImplementedError("`forward_rocm` not implemented!")

	def __call__(
		self,
		q: Array,
		k: Array,
		v: Array,
		mask: tp.Optional[Array] = None,
	) -> AttentionOutput:
		return super().__call__(q=q, k=k, v=v, mask=mask)


if __name__ == "__main__":
	from easydel.infra import EasyDeLBaseConfig

	# Test cace when qkv might refer to mla
	b, qs, ks, qh, kh, d, vd = 4, 1024, 1024, 32, 32, 128, 128

	q = jr.normal(jr.key(0), (b, qs, qh, d), "f4")
	k = jr.normal(jr.key(1), (b, ks, kh, d), "f4")
	v = jr.normal(jr.key(2), (b, ks, kh, vd), "f4")

	metadata = AttentionMetadata(
		runtime_dtype=jnp.bfloat16,
		base_config=EasyDeLBaseConfig(axis_dims=(1, -1, 1, 1)),
	)

	attn = SplashAttn(metadata)
	mask = jnp.repeat(attn._create_causal_mask(qs)[None, None, :, :], b, 0)
	vanilla = VanillaAttn(metadata)
	fout = attn(q=q, k=k, v=v, mask=mask).attention_outputs
	vout = vanilla(q=q, k=k, v=v, mask=mask).attention_outputs

	print(fout[-1, -1, -1, -5:], fout[-1, 0, -1, -5:])
	print(vout[-1, -1, -1, -5:], vout[-1, 0, -1, -5:])

	print(jnp.allclose(fout, vout, atol=0.125))

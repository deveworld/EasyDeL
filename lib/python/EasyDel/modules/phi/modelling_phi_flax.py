import functools
import warnings
from typing import Optional, Tuple, Any, Union, Dict, Sequence, Callable
import chex
import jax.lax
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput, FlaxMaskedLMOutput
from flax.core import FrozenDict, freeze, unfreeze
from flax.linen import combine_masks, dot_product_attention_weights, make_causal_mask
from flax.traverse_util import unflatten_dict, flatten_dict
from jax import numpy as jnp, lax
from flax import linen as nn
from chex import Array
from jax.experimental.shard_map import shard_map

from ..flax_modelling_utils import (
    ACT2FN,
    get_gradient_checkpoint_policy,
    canonicalize_dtype,
    apply_rotary_pos_emb,
    get_dot_general_by_bits, repeat_kv_bnsh, with_sharding_constraint, precompute_freq_cis
)
from .phi_configuration import PhiConfig
from ..easydel_modelling_utils import EasyDelFlaxPretrainedModel
from jax.sharding import PartitionSpec


class FlaxPhiEmbedding(nn.Module):
    dtype: jnp.dtype = jnp.float32

    def __call__(self, query, key, freq_cis, position_ids):
        sin, cos = freq_cis

        sin = sin[position_ids][:, None, :, :]
        cos = cos[position_ids][:, None, :, :]

        key = apply_rotary_pos_emb(key, sin, cos)
        query = apply_rotary_pos_emb(query, sin, cos)

        return query.astype(self.dtype), key.astype(self.dtype)


def repeat_kv(x: chex.Array, n_rep: int) -> chex.Array:
    bs, s, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    x = x[:, :, jnp.newaxis, :, :]
    x = jnp.repeat(x, n_rep, axis=2)

    return x.reshape(bs, s,
                     n_kv_heads * n_rep,
                     head_dim)


class FlaxPhiMLP(nn.Module):
    config: PhiConfig
    layer_idx: Optional[int] = None
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    """Multi-Layer Perceptron.
    Reference:
        Attention Is All You Need.
        https://arxiv.org/pdf/1706.03762.pdf.
    """

    def setup(
            self
    ) -> None:
        self.fc1 = nn.Dense(
            self.config.intermediate_size,
            kernel_init=nn.initializers.normal(self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.fc2 = nn.Dense(
            self.config.n_embd,
            kernel_init=nn.initializers.normal(self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.act = ACT2FN[self.config.hidden_act]

    def __call__(self, hidden_states: Array) -> Array:
        return self.fc2(self.act(self.fc1(hidden_states)))


class FlaxPhiAttention(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""
    config: PhiConfig
    layer_idx: Optional[int] = None
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self):
        config = self.config
        self.attention_dropout = config.attention_dropout
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.partial_rotary_factor = config.partial_rotary_factor
        self.is_causal = True

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )

        dense_class = functools.partial(
            nn.Dense,
            use_bias=True,
            precision=self.precision,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            **get_dot_general_by_bits(self.config.bits)
        )

        self.q_proj = dense_class(self.num_heads * self.head_dim)
        self.k_proj = dense_class(self.num_key_value_heads * self.head_dim)
        self.v_proj = dense_class(self.num_key_value_heads * self.head_dim)
        self.dense = dense_class(self.hidden_size)
        self.rotary = FlaxPhiEmbedding(self.dtype)
        self.qk_layernorm = config.qk_layernorm
        if self.qk_layernorm:
            self.q_layernorm = nn.LayerNorm(
                epsilon=config.layer_norm_eps,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                use_bias=True
            )
            self.k_layernorm = nn.LayerNorm(
                epsilon=config.layer_norm_eps,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                use_bias=True
            )

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.hidden_size,))

    @nn.compact
    def _concatenate_to_cache(self, key, value, query, attention_mask):
        """
        The _concatenate_to_cache function is used to concatenate the key and value vectors
        of a query with those of previous queries. This allows for the attention mechanism to
        look at all previous queries when computing its output. The function takes in three
        arguments: key, value, and query. It also uses two variables that are stored in the cache:
        cached_key and cached_value.

        :param self: Access the variables stored in the cache
        :param key: Store the keys of the encoder-decoder attention
        :param value: Initialize the cached_value variable
        :param query: Determine the number of cache vectors to update
        :param attention_mask: Mask out the padded vectors in the cache
        :return: The key, value and attention_mask
        """
        is_initialized = self.has_variable("cache", "cached_key")
        cached_key = self.variable(
            "cache", "cached_key", jnp.zeros, key.shape, key.dtype)
        cached_value = self.variable(
            "cache", "cached_value", jnp.zeros, value.shape, value.dtype)
        cache_index = self.variable(
            "cache", "cache_index", lambda: jnp.array(0, dtype=jnp.int32))

        if is_initialized:
            *batch_dims, max_length, num_heads, depth_per_head = cached_key.value.shape
            cur_index = cache_index.value
            indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
            key = lax.dynamic_update_slice(cached_key.value, key, indices)
            value = lax.dynamic_update_slice(
                cached_value.value, value, indices)
            cached_key.value = key
            cached_value.value = value
            num_updated_cache_vectors = query.shape[1]
            cache_index.value = cache_index.value + num_updated_cache_vectors

            pad_mask = jnp.broadcast_to(
                jnp.arange(max_length) < cur_index + num_updated_cache_vectors,
                tuple(batch_dims) + (1, num_updated_cache_vectors, max_length),
            )
            attention_mask = combine_masks(pad_mask, attention_mask)
        return key, value, attention_mask

    @staticmethod
    def _t(query, key, value):
        """
        The _t function transposes the query, key and value matrices.

        :param query: Get the attention weights for each of the heads
        :param key: Determine the number of heads
        :param value: Store the values of the input
        :return: The transpose of the query, key and value matrices

        """
        return jnp.transpose(query, (0, 2, 1, 3)), jnp.transpose(key, (0, 2, 1, 3)), jnp.transpose(value, (0, 2, 1, 3))

    def apply_rotary(self, batch_size, sequence_length, query, key, value, freq_cis, position_ids):
        """
        The apply_rotary function is a modified version of the apply_attention function in the BertModel class.
        The main difference is that it takes in an additional argument, freq_cis, which are used to calculate
        the rotary attention weights. The other differences are minor and mostly related to reshaping tensors.

        :param self: Access variables that belong to the class
        :param batch_size: Reshape the query, key and value tensors
        :param sequence_length: Reshape the query, key and value tensors
        :param query: Calculate the attention weights
        :param key: Calculate the attention
        :param value: Compute the attention weights
        :param freq_cis: Calculate the frequency of each word in the vocabulary
        :param position_ids: Identify the position of each token in the sequence
        :return: A tuple of 3 tensors: query, key and value

        """
        query = query.reshape(
            batch_size,
            sequence_length,
            self.config.num_attention_heads,
            self.head_dim
        )
        key = key.reshape(
            batch_size,
            sequence_length,
            self.config.num_key_value_heads,
            self.head_dim
        )
        value = value.reshape(
            batch_size,
            sequence_length,
            self.config.num_key_value_heads,
            self.head_dim
        )

        query, key, value = self._t(query, key, value)
        query, key = self.rotary(
            position_ids=position_ids, query=query, key=key, freq_cis=freq_cis
        )
        key = repeat_kv_bnsh(key, self.num_key_value_groups)
        value = repeat_kv_bnsh(value, self.num_key_value_groups)
        return self._t(query, key, value)

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: Tuple[chex.Array, chex.Array],
            attention_mask: Optional[chex.Array],
            position_ids: Optional[chex.Array],
            causal_mask: Optional[chex.Array],
            deterministic: bool = True,
            output_attentions: bool = False,
            init_cache: bool = False,
    ):
        batch_size, sequence_length = hidden_states.shape[:2]
        (
            query_states,
            key_states,
            value_states
        ) = self.q_proj(
            hidden_states
        ), self.k_proj(
            hidden_states
        ), self.v_proj(
            hidden_states
        )

        if self.qk_layernorm:
            query_states = self.q_layernorm(query_states)
            key_states = self.k_layernorm(key_states)

        if self.config.use_pjit_attention_force:
            query_states = with_sharding_constraint(
                query_states, PartitionSpec(("dp", "fsdp"), "sp", "tp")
            )
            key_states = with_sharding_constraint(
                key_states, PartitionSpec(("dp", "fsdp"), "sp", "tp")
            )
            value_states = with_sharding_constraint(
                value_states, PartitionSpec(("dp", "fsdp"), "sp", "tp")
            )

        query_states = query_states.reshape(
            batch_size, sequence_length, self.config.num_attention_heads, self.head_dim
        )
        key_states = key_states.reshape(
            batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim
        )
        value_states = value_states.reshape(
            batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim
        )

        query_states, key_states, value_states = self.apply_rotary(
            query=query_states,
            key=key_states,
            value=value_states,
            position_ids=position_ids,
            freq_cis=freq_cis,
            batch_size=batch_size,
            sequence_length=sequence_length
        )
        assert_msg = (
            "num_attention_heads repeat wont work likely\n"
            f"INFO :\n\trepeat_kv_bnsh Used with num_key_value_groups = {self.num_key_value_groups}\n\t"
            f"NH : {self.config.num_attention_heads} KVH : {self.config.num_attention_heads}"
        )

        assert query_states.shape[-2] == self.config.num_attention_heads, assert_msg
        assert key_states.shape[-2] == self.config.num_attention_heads, assert_msg
        assert value_states.shape[-2] == self.config.num_attention_heads, assert_msg

        query_length, key_length = query_states.shape[1], key_states.shape[1]

        if self.has_variable("cache", "cached_key"):
            mask_shift = self.variables["cache"]["cache_index"]
            max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
            causal_mask = lax.dynamic_slice(
                causal_mask, (0, 0, mask_shift, 0), (1, 1,
                                                     query_length, max_decoder_length)
            )
        else:
            causal_mask = causal_mask[:, :, :query_length, :key_length]

        batch_size = hidden_states.shape[0]
        causal_mask = jnp.broadcast_to(
            causal_mask, (batch_size,) + causal_mask.shape[1:])
        attention_mask = jnp.broadcast_to(jnp.expand_dims(
            attention_mask, axis=(-3, -2)), causal_mask.shape)
        attention_mask = combine_masks(attention_mask, causal_mask)
        if attention_mask.ndim == 2:
            attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

        dropout_rng = None

        if not deterministic and self.config.attention_dropout > 0.0:
            dropout_rng = self.make_rng("dropout")

        if self.has_variable("cache", "cached_key") or init_cache:
            key_states, value_states, attention_mask = self._concatenate_to_cache(
                key_states,
                value_states,
                query_states,
                attention_mask
            )
        attention_bias = lax.select(
            attention_mask > 0,
            jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
            jnp.full(attention_mask.shape, jnp.finfo(
                self.dtype).min).astype(self.dtype),
        )
        if self.config.use_shard_map:
            attn_weights = shard_map(
                functools.partial(
                    dot_product_attention_weights,
                    dtype=jnp.promote_types(self.dtype, jnp.float32),
                    deterministic=deterministic,
                    dropout_rate=self.config.attention_dropout,
                    precision=self.precision,
                    dropout_rng=dropout_rng
                ),
                mesh=self.config.jax_mesh(),
                in_specs=(
                    self.config.query_partition_spec,
                    self.config.key_partition_spec,
                    self.config.bias_partition_spec
                ),
                out_specs=PartitionSpec(("dp", "fsdp"), "sp", "tp", None),
                check_rep=False
            )(
                query_states, key_states, attention_bias
            )
        else:
            attn_weights = dot_product_attention_weights(
                query=query_states,
                key=key_states,
                bias=attention_bias,
                dtype=jnp.promote_types(self.dtype, jnp.float32),
                deterministic=deterministic,
                dropout_rate=self.config.attention_dropout,
                precision=self.precision,
                dropout_rng=dropout_rng
            )

        if self.config.use_pjit_attention_force:
            attn_weights = with_sharding_constraint(
                attn_weights, PartitionSpec(("dp", "fsdp"), "sp", "tp", None))

        attn_output = jnp.einsum(
            "...hqk,...khd->...qhd",
            attn_weights,
            value_states,
            precision=self.precision
        )
        attn_output = self._merge_heads(attn_output)
        attn_output = self.dense(attn_output)

        outputs = (attn_output, attn_weights) if output_attentions else (attn_output,)
        return outputs


class FlaxPhiDecoderLayer(nn.Module):
    config: PhiConfig
    layer_idx: int
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self):
        self.self_attn = FlaxPhiAttention(
            config=self.config,
            layer_idx=self.layer_idx,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.mlp = FlaxPhiMLP(
            config=self.config,
            layer_idx=self.layer_idx,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.input_layernorm = nn.LayerNorm(
            epsilon=self.config.layer_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )
        self.resid_dropout = nn.Dropout(self.config.resid_pdrop)

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: Tuple[chex.Array, chex.Array],
            attention_mask: Optional[chex.Array],
            position_ids: Optional[chex.Array],
            causal_mask: Optional[chex.Array],
            deterministic: bool = True,
            output_attentions: bool = False,
            init_cache: bool = False,
    ):
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)

        attn_out = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            output_attentions=output_attentions,
            deterministic=deterministic,
            freq_cis=freq_cis,
            causal_mask=causal_mask,
            init_cache=init_cache,
        )
        attn_outputs, self_attn_weights = attn_out if len(attn_out) == 2 else attn_out[0], None
        attn_outputs = self.resid_dropout(attn_outputs, deterministic=deterministic)

        feed_forward_hidden_states = self.resid_dropout(self.mlp(hidden_states), deterministic=deterministic)
        hidden_states = attn_outputs + feed_forward_hidden_states + residual
        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        return outputs


class FlaxPhiDecoderLayerCollection(nn.Module):
    config: PhiConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        self.layers = [
            FlaxPhiDecoderLayer(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(idx),
                layer_idx=idx
            )
            for idx in range(self.config.num_hidden_layers)
        ]

    def __call__(
            self,
            hidden_states: chex.Array,
            freq_cis: Tuple[chex.Array, chex.Array],
            attention_mask: Optional[chex.Array],
            position_ids: Optional[chex.Array],
            causal_mask: Optional[chex.Array],
            deterministic: bool = True,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            init_cache: bool = False,
            return_dict: bool = True,
    ) -> tuple[tuple, ...] | FlaxBaseModelOutput:
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        for decoder_layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            layer_outputs = decoder_layer(
                hidden_states=hidden_states,
                freq_cis=freq_cis,
                attention_mask=attention_mask,
                causal_mask=causal_mask,
                position_ids=position_ids,
                output_attentions=output_attentions,
                deterministic=deterministic,
                init_cache=init_cache,
            )

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        if not return_dict:
            return tuple(v for v in [hidden_states, all_hidden_states, all_self_attns] if v is not None)
        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


class FlaxPhiModule(nn.Module):
    config: PhiConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        config = self.config
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.embed_tokens = nn.Embed(
            config.vocab_size,
            config.hidden_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.embed_dropout = nn.Dropout(config.embd_pdrop)
        self.layers = FlaxPhiDecoderLayerCollection(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.final_layernorm = nn.LayerNorm(
            epsilon=config.layer_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.causal_mask = make_causal_mask(
            jnp.ones(
                (1, config.max_position_embeddings)
            )
        )

        initial_rope_kwargs = dict(
            rope_type="none"
        )
        if hasattr(config, "rope_scaling"):
            if config.rope_scaling is not None:
                scaling_type = config.rope_scaling["type"]
                scaling_factor = config.rope_scaling["factor"]
                initial_rope_kwargs = dict(
                    scaling_factor=scaling_factor,
                    rope_type=scaling_type
                )
        self.freq_cis = precompute_freq_cis(
            max_position_embeddings=config.max_position_embeddings,
            dim=config.hidden_size // config.num_attention_heads,
            base=config.rope_theta,
            **initial_rope_kwargs
        )

    def __call__(
            self,
            input_ids: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            attention_mask: Optional[chex.Array] = None,
            position_ids: Optional[chex.Array] = None,
            extra_embedding: Optional[chex.Array] = None,
            deterministic: bool = True,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            init_cache: bool = False,
            return_dict: bool = True,
    ) -> tuple[tuple[Any, ...], ...] | FlaxBaseModelOutput:
        if input_ids is None and inputs_embeds is None:
            raise RuntimeError("Both `input_ids` and `inputs_embeds` can not be None !")
        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids.astype("i4"))
        inputs_embeds = self.embed_dropout(inputs_embeds, deterministic=deterministic)
        batch_size, sequence_length, _ = inputs_embeds.shape
        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length), dtype="i4")
        if position_ids is None:
            position_ids = (jnp.cumsum(attention_mask) - 1).reshape(batch_size, sequence_length).astype("i4")
        assert sequence_length <= self.config.max_position_embeddings, "Maximum Position Embedding Reached !"
        inputs_embeds = inputs_embeds + extra_embedding if extra_embedding is not None else inputs_embeds

        outputs = self.layers(
            hidden_states=inputs_embeds,
            freq_cis=self.freq_cis,
            attention_mask=attention_mask,
            position_ids=position_ids,
            causal_mask=self.causal_mask,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        hidden_states = self.final_layernorm(hidden_states)

        if output_hidden_states:
            all_hidden_states = outputs[1] + (hidden_states,)
            outputs = (hidden_states, all_hidden_states) + outputs[2:]
        else:
            outputs = (hidden_states,) + outputs[1:]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs[1] if output_hidden_states else None,
            attentions=outputs[-1] if output_attentions else None,
        )


class FlaxPhiForCausalLMModule(nn.Module):
    config: PhiConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest")

    def setup(self) -> None:
        self.model = FlaxPhiModule(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.vocab_size = self.config.vocab_size
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            use_bias=True,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(
            self,
            input_ids: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            attention_mask: Optional[chex.Array] = None,
            position_ids: Optional[chex.Array] = None,
            extra_embedding: Optional[chex.Array] = None,
            deterministic: bool = True,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            init_cache: bool = False,
            return_dict: bool = True,
    ) -> tuple[Any, ...] | FlaxMaskedLMOutput:
        res = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            init_cache=init_cache,
            deterministic=deterministic,
            extra_embedding=extra_embedding,
            position_ids=position_ids,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True
        )
        outputs = (res.last_hidden_state, res.hidden_states, res.attentions)
        if self.config.tie_word_embeddings:
            shared_kernel = self.model.variables["params"]["embed_tokens"]["embedding"].T
            lm_logits = self.lm_head.apply(
                {"params": {"kernel": shared_kernel}}, res.last_hidden_state)
        else:
            lm_logits = self.lm_head(res.last_hidden_state)

        lm_logits = lm_logits.astype(jnp.float32)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxCausalLMOutput(logits=lm_logits, hidden_states=res.hidden_states, attentions=res.attentions)


class FlaxPhiPreTrainedModel(EasyDelFlaxPretrainedModel):
    """Phi pre-trained model."""
    module_class = None
    config_class = PhiConfig
    base_model_prefix = "transformer"

    def __init__(
            self,
            config: PhiConfig,
            dtype: jnp.dtype = jnp.float32,
            param_dtype: jnp.dtype = jnp.float32,
            precision: Optional[jax.lax.Precision] = jax.lax.Precision("fastest"),
            input_shape=(1, 1),
            seed: int = 42,
            _do_init: bool = False
    ) -> None:
        module = self.module_class(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision
        )
        super().__init__(
            config=config,
            module=module,
            input_shape=input_shape,
            _do_init=_do_init,
            seed=seed
        )

    def init_cache(self, batch_size, max_length):

        input_ids = jnp.ones((batch_size, max_length))
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(
            jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, position_ids, return_dict=False, init_cache=True
        )
        return init_variables["cache"]

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple, params: FrozenDict = None) -> FrozenDict:
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        module_init_outputs = self.module.init(rngs, input_ids, attention_mask)

        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def __call__(
            self,
            input_ids: chex.Array,
            attention_mask: chex.Array = None,
            position_ids: chex.Array = None,
            params: dict = None,
            past_key_values: dict = None,
            dropout_rng: jax.random.PRNGKey = None,
            train: bool = False,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = True,
            extra_embedding: Optional[Union[jnp.ndarray, None]] = None,
            add_params_field: bool = False,
            **kwargs
    ):

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        assert sequence_length <= self.config.max_position_embeddings, "Maximum Position Embedding Reached !"

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))

        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        if self.config.bits is not None:
            rngs['params'] = jax.random.key(0)

        inputs = {"params": params or self.params} if add_params_field else params or self.params

        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            input_ids=input_ids,
            inputs_embeds=None,
            attention_mask=attention_mask,
            position_ids=position_ids,
            extra_embedding=extra_embedding,
            deterministic=not train,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            init_cache=False,
            return_dict=return_dict,
            rngs=rngs,
            mutable=mutable,
        )

        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxPhiModel(FlaxPhiPreTrainedModel):
    module_class = FlaxPhiModule


class FlaxPhiForCausalLM(FlaxPhiPreTrainedModel):
    module_class = FlaxPhiForCausalLMModule

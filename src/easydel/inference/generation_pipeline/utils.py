import dataclasses
from functools import partial
from typing import Dict, Optional, Union

import jax
import jax.random
from jax import numpy as jnp
from jax import random, sharding

from easydel.generation.logits_process import (
    FlaxTemperatureLogitsWarper,
    FlaxTopKLogitsWarper,
    FlaxTopPLogitsWarper,
)


class GenerationPipelineConfig:
    """
    Configuration class for the text generation pipeline.

    Attributes:
        max_new_tokens: Maximum number of tokens to generate.
        temperature: Temperature parameter for sampling.
        top_p: Top-p (nucleus) sampling threshold.
        top_k: Top-k sampling parameter.
        repetition_penalty: Penalty for repeating tokens.
        length_penalty: Penalty for generating longer sequences.
        pad_token_id: ID of the padding token.
        bos_token_id: ID of the beginning-of-sequence token.
        eos_token_id: ID of the end-of-sequence token.
    """

    def __init__(
        self,
        max_new_tokens: int = 64,
        temperature: float = 0.0,
        top_p: float = 0.95,
        top_k: int = 50,
        repetition_penalty: float = 1.0,
        length_penalty: float = 1.0,
        pad_token_id: Optional[int] = None,
        bos_token_id: Optional[int] = None,
        eos_token_id: Optional[int] = None,
        **kwargs,
    ):
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repetition_penalty = repetition_penalty
        self.length_penalty = length_penalty
        self.pad_token_id = pad_token_id
        self.bos_token_id = bos_token_id
        self.eos_token_id = eos_token_id

    def __hash__(self) -> int:
        int_hash = int(
            (
                "---".join(
                    str(cu)
                    for cu in self.__dict__.values()
                    if isinstance(cu, (float, int))
                )
            )
            .replace("---", "")
            .replace(".", "")
        )

        return int_hash


class _DynamicGenerationConfig:
    """
    Dynamic configuration class for the text generation pipeline.

    This class holds the subset of generation parameters that can be
    dynamically updated during the generation process.

    Attributes:
        temperature: Temperature parameter for sampling.
        top_k: Top-k sampling parameter.
        top_p: Top-p (nucleus) sampling threshold.
        repetition_penalty: Penalty for repeating tokens.
        length_penalty: Penalty for generating longer sequences.
    """

    def __init__(self, config):
        self.temperature = config.temperature
        self.top_k = config.top_k
        self.top_p = config.top_p
        self.repetition_penalty = config.repetition_penalty
        self.length_penalty = config.length_penalty


def compile_function(
    func,
    func_input_args,
    func_input_kwargs,
    mesh=None,
    in_shardings=None,
    out_shardings=None,
    static_argnums=None,
    donate_argnums=None,
):
    """
    Compiles a JAX function with optional sharding and mesh configuration.

    Args:
        func: The JAX function to compile.
        func_input_args: Input arguments for the function.
        func_input_kwargs: Input keyword arguments for the function.
        mesh: Optional JAX mesh for distributed execution.
        in_shardings: Optional input sharding specifications.
        out_shardings: Optional output sharding specifications.
        static_argnums: Indices of static arguments.
        donate_argnums: Indices of arguments to donate.

    Returns:
        Compiled JAX function.
    """
    if mesh is None:
        return (
            jax.jit(
                func,
                in_shardings=in_shardings,
                out_shardings=out_shardings,
                static_argnums=static_argnums,
                donate_argnums=donate_argnums,
            )
            .lower(*func_input_args, **func_input_kwargs)
            .compile()
        )
    with mesh:
        return (
            jax.jit(
                func,
                in_shardings=in_shardings,
                out_shardings=out_shardings,
                static_argnums=static_argnums,
                donate_argnums=donate_argnums,
            )
            .lower(*func_input_args, **func_input_kwargs)
            .compile()
        )


@jax.tree_util.register_pytree_node_class
@dataclasses.dataclass
class SampleState:
    """
    Data class representing the state of the sampling process.

    Attributes:
        cur_len: Current length of the generated sequence.
        sequences: Generated token sequences.
        running_token: The last generated token for each sequence.
        is_sent_finished: Boolean array indicating finished sequences.
        prng_key: JAX PRNG key for random sampling.
        model_kwargs: Keyword arguments passed to the model.
    """

    cur_len: Union[jax.Array, sharding.NamedSharding]
    sequences: Union[jax.Array, sharding.NamedSharding]
    running_token: Union[jax.Array, sharding.NamedSharding]
    is_sent_finished: Union[jax.Array, sharding.NamedSharding]
    prng_key: Union[random.PRNGKey, sharding.NamedSharding]
    model_kwargs: Union[Dict[str, jax.Array], sharding.NamedSharding]

    def tree_flatten(self):
        return (
            self.cur_len,
            self.sequences,
            self.running_token,
            self.is_sent_finished,
            self.prng_key,
            self.model_kwargs,
        ), {}

    @classmethod
    def tree_unflatten(cls, aux, children):
        return cls(*children)


def apply_repetition_penalty(logits, tokens, penalty):
    """
    Applies repetition penalty to the logits.

    Args:
        logits: Logits tensor.
        tokens: Previously generated tokens.
        penalty: Repetition penalty factor.

    Returns:
        Logits tensor with repetition penalty applied.
    """

    # Create a mask for the tokens that appear in the input
    vocab_size = logits.shape[-1]
    token_mask = jnp.zeros(vocab_size, dtype=jnp.bool_)
    token_mask = token_mask.at[tokens].set(True)

    # Apply the penalty
    logits = jnp.where(token_mask, logits / penalty, logits * penalty)

    return logits


def apply_length_penalty(logits, cur_len, max_len, length_penalty):
    """
    Applies length penalty to the logits.

    Args:
        logits: Logits tensor.
        cur_len: Current length of the generated sequence.
        max_len: Maximum length of the sequence.
        length_penalty: Length penalty factor.

    Returns:
        Logits tensor with length penalty applied.
    """

    # Calculate the penalty factor
    penalty_factor = ((5 + cur_len) / 6) ** length_penalty

    # Apply the penalty
    return logits / penalty_factor


@partial(jax.jit, static_argnames=["top_k"])
def apply_top_k_sampling(logits, top_k):
    """
    Applies top-k sampling to the logits.

    Args:
        logits: Logits tensor.
        top_k: Number of top logits to consider.

    Returns:
        Logits tensor with top-k sampling applied.
    """
    batch_size, vocab_size = logits.shape
    next_logits_flat = jnp.full(batch_size * vocab_size, -float("Inf"))

    topk = min(top_k, logits.shape[-1])  # Safety check
    topk_logits, topk_indices = jax.lax.top_k(logits, topk)
    shift = jnp.broadcast_to(
        (jnp.arange(batch_size) * vocab_size)[:, None], (batch_size, topk)
    ).flatten()
    topk_logits_flat = topk_logits.flatten()
    topk_indices_flat = topk_indices.flatten() + shift

    next_logits_flat = next_logits_flat.at[topk_indices_flat].set(topk_logits_flat)
    next_logits = next_logits_flat.reshape(batch_size, vocab_size)
    return next_logits


def apply_top_p_sampling(logits, top_p):
    """
    Applies top-p (nucleus) sampling to the logits.

    Args:
        logits: Logits tensor.
        top_p: Top-p sampling threshold.

    Returns:
        Logits tensor with top-p sampling applied.
    """
    topk_logits, topk_indices = jax.lax.top_k(logits, logits.shape[-1])

    mask_logits = jnp.full_like(logits, -float("Inf"))
    cumulative_probs = jax.nn.softmax(topk_logits, axis=-1).cumsum(axis=-1)
    score_mask = cumulative_probs < top_p
    score_mask = jnp.roll(score_mask, 1)
    score_mask |= score_mask.at[:, 0].set(True)
    score_mask = score_mask.at[:, :1].set(True)

    topk_next_logits = jnp.where(score_mask, topk_logits, mask_logits)
    next_logits = jax.lax.sort_key_val(topk_indices, topk_next_logits)[-1]

    return next_logits


def sampling(sampling_logits, key):
    """
    Samples from the logits using categorical distribution.

    Args:
        sampling_logits: Logits tensor.
        key: JAX PRNG key.

    Returns:
        Sampled token IDs.
    """
    return jax.random.categorical(key, sampling_logits).reshape(-1)


def temperature_branch(logits, prng_key, top_k, temperature, top_p):
    """
    Applies temperature scaling, top-k and top-p sampling to the logits.

    Args:
        logits: Logits tensor.
        prng_key: JAX PRNG key.
        top_k: Number of top logits to consider.
        temperature: Temperature scaling factor.
        top_p: Top-p sampling threshold.

    Returns:
        Sampled token IDs.
    """
    logits = FlaxTemperatureLogitsWarper(temperature=temperature)(None, logits, None)
    if top_k > 1:
        logits = FlaxTopKLogitsWarper(top_k=top_k)(None, logits, None)
    if 0 < top_p < 1.0:
        logits = FlaxTopPLogitsWarper(top_p=top_p)(None, logits, None)
    return jax.random.categorical(key=prng_key, logits=logits)


def gready_branch(logits):
    """
    Performs greedy decoding on the logits.

    Args:
        logits: Logits tensor.

    Returns:
        Token IDs with the highest logits.
    """
    return jnp.argmax(logits, axis=-1).reshape(-1)


def inference_step(
    logits,
    tokens,
    prng_key,
    config,
    cur_len,
    max_length,
):
    """
    Performs a single inference step in the text generation process.

    This function applies repetition and length penalties to the logits,
    and then performs either temperature-based sampling or greedy decoding.

    Args:
        logits: Model's logits for the current step.
        tokens: Previously generated tokens.
        prng_key: JAX PRNG key for random sampling.
        config: GenerationPipelineConfig object.
        cur_len: Current length of the generated sequence.
        max_length: Maximum allowed length for the generated sequence.

    Returns:
        jax.Array: An array of generated token IDs.
    """
    length_penalty = config.length_penalty
    repetition_penalty = config.repetition_penalty
    # Apply repetition penalty
    logits = jax.lax.cond(
        repetition_penalty != 1.0,
        apply_repetition_penalty,
        lambda x, *u: x,
        logits,
        tokens,
        repetition_penalty,
    )

    # Apply length penalty
    logits = jax.lax.cond(
        length_penalty != 1.0,
        apply_length_penalty,
        lambda x, *u: x,
        logits,
        cur_len,
        max_length,
        length_penalty,
    )

    if config.temperature > 0.0:
        return temperature_branch(
            logits=logits,
            prng_key=prng_key,
            top_k=config.top_k,
            top_p=config.top_p,
            temperature=config.temperature,
        )
    return gready_branch(logits=logits)


inference_step_compiled = jax.jit(
    inference_step,
    static_argnames=["max_length", "config"],
)

"""
Usage Example:

python sft_trainer.py \
    --repo_id meta-llama/Llama-3.1-70B-Instruct \
    --dataset_name trl-lib/Capybara \
    --dataset_split train \
    --dataset_text_field messages \
    --max_length 2048 \
    --num_train_epochs 1 \
    --total_batch_size 8 \
    --learning_rate 1e-5 \
    --warmup_steps 50 \
    --optimizer ADAMW \
    --scheduler COSINE \
    --sharding_axis_dims "1,-1,1,1" \ 
    --save_steps 1000
"""

import argparse
from datasets import load_dataset
from transformers import AutoTokenizer
from jax import numpy as jnp
import jax
import easydel as ed


def parse_args():
	parser = argparse.ArgumentParser(
		description="Train a model using EasyDeL for supervised fine-tuning (SFT)."
	)

	# Model configuration
	parser.add_argument(
		"--repo_id",
		type=str,
		default="meta-llama/Llama-3.1-70B-Instruct",
		help="Hugging Face repository ID for the model and tokenizer.",
	)
	parser.add_argument(
		"--max_length",
		type=int,
		default=2048,
		help="Maximum sequence length for model inputs.",
	)
	parser.add_argument(
		"--sharding_axis_dims",
		type=str,
		default="1,-1,1,1",
		help="Sharding dimensions for model parallelism as comma-separated integers.",
	)

	# Dataset configuration
	parser.add_argument(
		"--dataset_name",
		type=str,
		default="trl-lib/Capybara",
		help="Name of the dataset to load from Hugging Face Hub.",
	)
	parser.add_argument(
		"--dataset_split",
		type=str,
		default="train",
		help="Dataset split to use for training.",
	)
	parser.add_argument(
		"--dataset_text_field",
		type=str,
		default="messages",
		help="Field in the dataset containing the text for training.",
	)

	# Training configuration
	parser.add_argument(
		"--num_train_epochs", type=int, default=1, help="Number of training epochs."
	)
	parser.add_argument(
		"--total_batch_size",
		type=int,
		default=8,
		help="Total batch size across all devices.",
	)
	parser.add_argument(
		"--learning_rate", type=float, default=1e-5, help="Learning rate for the optimizer."
	)
	parser.add_argument(
		"--warmup_steps",
		type=int,
		default=50,
		help="Number of warmup steps for the learning rate scheduler.",
	)

	# Optimization configuration
	parser.add_argument(
		"--optimizer",
		type=str,
		default="ADAMW",
		choices=[opt.name for opt in ed.EasyDeLOptimizers],
		help="Optimizer to use for training.",
	)
	parser.add_argument(
		"--scheduler",
		type=str,
		default="COSINE",
		choices=[sched.name for sched in ed.EasyDeLSchedulers],
		help="Learning rate scheduler to use.",
	)

	# Logging and saving
	parser.add_argument(
		"--log_steps", type=int, default=1, help="Number of steps between logging metrics."
	)
	parser.add_argument(
		"--save_steps",
		type=int,
		default=1000,
		help="Number of steps between checkpoint saves.",
	)
	parser.add_argument(
		"--save_total_limit",
		type=int,
		default=1,
		help="Maximum number of checkpoints to keep.",
	)
	parser.add_argument(
		"--progress_bar_type",
		type=str,
		default="tqdm",
		choices=["tqdm", "json"],
		help="Type of progress bar to use.",
	)

	# Boolean flags
	parser.add_argument(
		"--no_do_last_save",
		action="store_false",
		dest="do_last_save",
		help="Disable saving the final checkpoint.",
	)
	parser.add_argument(
		"--no_use_wandb",
		action="store_false",
		dest="use_wandb",
		help="Disable Weights & Biases logging.",
	)
	parser.add_argument(
		"--save_optimizer_state",
		action="store_true",
		help="Save optimizer state with checkpoints.",
	)
	parser.add_argument(
		"--no_process_zero_is_admin",
		action="store_false",
		dest="process_zero_is_admin",
		help="Disable process zero admin privileges.",
	)
	parser.add_argument(
		"--packing", action="store_true", help="Enable packing for training sequences."
	)

	return parser.parse_args()


def formatting_prompts_func(example, tokenizer):
	return [tokenizer.apply_chat_template(example["conversation"], tokenize=False)]


def main():
	args = parse_args()

	# Convert string arguments to appropriate types
	sharding_axis_dims = tuple(map(int, args.sharding_axis_dims.split(",")))
	optimizer = getattr(ed.EasyDeLOptimizers, args.optimizer)
	scheduler = getattr(ed.EasyDeLSchedulers, args.scheduler)

	# Load tokenizer
	tokenizer = AutoTokenizer.from_pretrained(args.repo_id)
	tokenizer.padding_side = "left"

	if tokenizer.pad_token_id is None:
		tokenizer.pad_token_id = tokenizer.eos_token_id

	# Load dataset
	dataset = load_dataset(args.dataset_name, split=args.dataset_split)

	# Initialize model
	model = ed.AutoEasyDeLModelForCausalLM.from_pretrained(
		args.repo_id,
		auto_shard_model=True,
		sharding_axis_dims=sharding_axis_dims,
		config_kwargs=ed.EasyDeLBaseConfigDict(
			freq_max_position_embeddings=args.max_length,
			mask_max_position_embeddings=args.max_length,
			attn_dtype=jnp.bfloat16,
			attn_softmax_dtype=jnp.float32,
			gradient_checkpointing=ed.EasyDeLGradientCheckPointers.NOTHING_SAVEABLE,
			kv_cache_quantization_method=ed.EasyDeLQuantizationMethods.NONE,
			attn_mechanism=ed.AttentionMechanisms.VANILLA,
		),
		quantization_method=ed.EasyDeLQuantizationMethods.NONE,
		platform=ed.EasyDeLPlatforms.JAX,
		param_dtype=jnp.bfloat16,
		dtype=jnp.bfloat16,
		precision=jax.lax.Precision("fastest"),
		partition_axis=ed.PartitionAxis(),
	)

	# Initialize trainer
	trainer = ed.SFTTrainer(
		model=model,
		arguments=ed.SFTConfig(
			dataset_text_field=args.dataset_text_field,
			max_sequence_length=args.max_length,
			num_train_epochs=args.num_train_epochs,
			total_batch_size=args.total_batch_size,
			log_steps=args.log_steps,
			do_last_save=args.do_last_save,
			packing=args.packing,
			use_wandb=args.use_wandb,
			save_optimizer_state=args.save_optimizer_state,
			progress_bar_type=args.progress_bar_type,
			save_steps=args.save_steps,
			save_total_limit=args.save_total_limit,
			learning_rate=args.learning_rate,
			optimizer=optimizer,
			scheduler=scheduler,
			warmup_steps=args.warmup_steps,
			process_zero_is_admin=args.process_zero_is_admin,
		),
		train_dataset=dataset,
		processing_class=tokenizer,
		formatting_func=lambda x: tokenizer.apply_chat_template(
			x[args.dataset_text_field],
			tokenize=False,
		),
	)

	# Start training
	trainer.train()


if __name__ == "__main__":
	main()

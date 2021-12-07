#!/bin/bash
if [ -z "$1" ]
then
    echo "Missing an experiment id"
    exit 1
fi

experiment=poet_$1

mkdir -p ./ipp/$experiment
mkdir -p ./logs/$experiment
mkdir -p ./models/$experiment

python -u master.py \
  ./logs/$experiment \
  --model_dir=./models/$experiment \
  --init=random \
  --learning_rate=0.01 \
  --lr_decay=0.9999 \
  --lr_limit=0.001 \
  --batch_size=1 \
  --batches_per_chunk=256 \
  --eval_batch_size=1 \
  --eval_batches_per_step=5 \
  --master_seed=24582922 \
  --noise_std=0.1 \
  --noise_decay=0.999 \
  --noise_limit=0.01 \
  --normalize_grads_by_noise_std \
  --returns_normalization=centered_ranks \
  --envs stump pit roughness \
  --max_num_envs=14 \
  --adjust_interval=1 \
  --propose_with_adam \
  --steps_before_transfer=10 \
  --num_workers 64 \
  --max_num_morphs 5 \
  --morph_evolve_interval 4 \
  --n_iterations=50000 2>&1 | tee ./ipp/$experiment/run.log

cd ../model
export EXPERT_DATA=/home/frosa_loc/Multi-Task-LFD-Framework/repo/TOSIL/one_shot_transformers/dataset
export CUDA_VISIBLE_DEVICES=1
python ../scripts/train_transformer.py ../experiments/base.yaml

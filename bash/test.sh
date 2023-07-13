cd ../model
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/home/frosa_loc/.mujoco/mujoco210/bin
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/nvidia
export EXPERT_DATA=/home/frosa_loc/Multi-Task-LFD-Framework/repo/TOSIL/one_shot_transformers/dataset
export CUDA_VISIBLE_DEVICES=0
MODEL_PATH=/home/frosa_loc/Multi-Task-LFD-Framework/repo/TOSIL/one_shot_transformers/model/bc_inv_ckpt-17-2_25-5-2023/model_save-160000.pt

python ../scripts/test_transformers.py ${MODEL_PATH} --N 160 --num_workers 2

cd ../bash

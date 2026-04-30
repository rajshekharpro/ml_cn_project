# ml_cn_project Workflow Guide

This project combines two related codebases:

- `ET-BERT/`: data preparation, pretraining, fine-tuning, and inference for encrypted traffic modeling
- `demystifying-networks-main/`: embedding export and intrinsic evaluation for comparing models

This README is a practical handoff guide for teammates. It explains what each stage does, which directory to run commands from, what files should appear after each step, and how to run the workflow both locally and on IITD HPC.

## Repository Layout

```text
ml_cn_project/
├── ET-BERT/
│   ├── data_process/
│   ├── pre-training/
│   ├── finetuning/
│   ├── inference/
│   ├── models/
│   ├── corpora/
│   └── datasets/
├── demystifying-networks-main/
│   ├── src/
│   ├── embeddings/
│   └── data/
└── README.md
```

## Working Rules

- Run ET-BERT commands from `ET-BERT/`.
- Run evaluation commands from `demystifying-networks-main/src/`.
- The same dataset usually moves through these stages:
  1. raw PCAPs
  2. split session PCAPs
  3. labeled TSV datasets for classifier training
  4. unlabeled burst corpus for ET-BERT pretraining
  5. `dataset.pt`
  6. pretrained ET-BERT checkpoint
  7. fine-tuned classifier or exported embeddings

## Required Tools

At minimum, make sure the environment has:

- Python 3.9 or 3.10
- PyTorch
- `scapy`
- `flowcontainer`
- `numpy`, `pandas`, `tqdm`, `scikit-learn`
- `mono` for `SplitCap.exe`
- `editcap` if converting `pcapng` to `pcap`

Project requirement files:

- root: `requirements.txt`
- ET-BERT: `ET-BERT/requirements.txt`

## End-to-End Workflow

### Stage 1: Split Large PCAPs into Session PCAPs

Purpose:
- Take large raw traffic captures and split them into many small session-level PCAP files.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 data_process/main.py --pcap_dir ../datasets/college_data/ --splitcap True
```

Expected result:

- `ET-BERT/datasets/college_data/splitcap/`
- one subfolder per class label, for example `splitcap/0/`
- many small `.pcap` files inside each label folder

Success check:

```bash
find datasets/college_data/splitcap -type f | head
```

Notes:

- If raw PCAP files are directly inside the dataset root, the script may auto-move them into a class folder such as `0/`.
- In this project, `SplitCap.exe` is expected at `ET-BERT/SplitCap.exe`.

### Stage 2: Generate Labeled TSV Datasets

Purpose:
- Convert the split session PCAPs into ET-BERT-ready TSV files for supervised classification.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 data_process/main.py --pcap_dir ../datasets/college_data/splitcap/
```

Expected result:

- `ET-BERT/datasets/college_data/train_dataset.tsv`
- `ET-BERT/datasets/college_data/valid_dataset.tsv`
- `ET-BERT/datasets/college_data/test_dataset.tsv`
- `ET-BERT/datasets/college_data/nolabel_test_dataset.tsv`
- `ET-BERT/datasets/college_data/dataset/`
- `ET-BERT/datasets/college_data/dataset.json`
- `ET-BERT/datasets/college_data/picked_file_record`

Important note:

- The `.tsv` files are written into `datasets/college_data/`.
- The cached `.npy` arrays are written into `datasets/college_data/dataset/`.

Success check:

```bash
ls -lh datasets/college_data/*.tsv
ls -lh datasets/college_data/dataset/
```

### Stage 3: Generate the Pretraining Text Corpus

Purpose:
- Create `encrypted_burst.txt`, the unlabeled burst-based text corpus used by ET-BERT pretraining.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 data_process/dataset_generation.py --pcap_dir ../datasets/college_data/splitcap/ --pretrain
```

Expected result:

- `ET-BERT/corpora/encrypted_burst.txt`

Success check:

```bash
ls -lh corpora/encrypted_burst.txt
head -n 5 corpora/encrypted_burst.txt
```

Important note:

- Use `data_process/dataset_generation.py --pretrain` for this stage.
- Do not use `data_process/main.py` for corpus generation.

### Stage 4: Convert the Corpus into `dataset.pt`

Purpose:
- Preprocess the text corpus into the PyTorch dataset format used by ET-BERT pretraining.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 preprocess.py \
  --corpus_path corpora/encrypted_burst.txt \
  --vocab_path models/encryptd_vocab.txt \
  --dataset_path dataset.pt \
  --processes_num 20 \
  --target bert
```

Expected result:

- `ET-BERT/dataset.pt`

Success check:

```bash
ls -lh dataset.pt
```

### Stage 5: Pretrain or Continue Training ET-BERT

Purpose:
- Train ET-BERT on the burst corpus, either from scratch or starting from an existing checkpoint.

Run from:

```bash
cd ET-BERT
```

#### Local Debug Run

Useful for quick validation that the pipeline works.

```bash
OMP_NUM_THREADS=8 python3 pre-training/pretrain.py \
  --dataset_path dataset.pt \
  --vocab_path models/encryptd_vocab.txt \
  --output_model_path models/campus_pre-trained_model.bin \
  --total_steps 10 \
  --save_checkpoint_steps 10 \
  --batch_size 4 \
  --accumulation_steps 8 \
  --instances_buffer_size 2560 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible \
  --target bert
```

Expected result:

- `ET-BERT/models/campus_pre-trained_model.bin-10`

#### Continue Training from Existing ET-BERT Weights

```bash
OMP_NUM_THREADS=24 python3 pre-training/pretrain.py \
  --dataset_path dataset.pt \
  --vocab_path models/encryptd_vocab.txt \
  --pretrained_model_path models/pre-trained_model.bin \
  --output_model_path models/my_campus_pretrained.bin \
  --total_steps 50 \
  --save_checkpoint_steps 50 \
  --batch_size 32 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible \
  --target bert
```

Expected result:

- `ET-BERT/models/my_campus_pretrained.bin-50`

Important note:

- ET-BERT saves checkpoints with a step suffix.
- If `--output_model_path` is `models/foo.bin` and `--save_checkpoint_steps 1000`, then the file created is `models/foo.bin-1000`.

### Stage 6: Fine-Tune the Classifier

Purpose:
- Train a supervised classifier on top of ET-BERT using the labeled TSV datasets.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 finetuning/run_classifier.py \
  --pretrained_model_path models/campus_pre-trained_model.bin-10 \
  --vocab_path models/encryptd_vocab.txt \
  --train_path datasets/college_data/train_dataset.tsv \
  --dev_path datasets/college_data/valid_dataset.tsv \
  --test_path datasets/college_data/test_dataset.tsv \
  --epochs_num 10 \
  --batch_size 32 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible \
  --seq_length 128 \
  --learning_rate 2e-5
```

Expected result:

- `ET-BERT/models/finetuned_model.bin`
- dev accuracy printed during training
- test accuracy and confusion matrix printed at the end

Important note:

- The correct folder is `finetuning/`, not `fine-tuning/`.

### Stage 7: Run Inference

Purpose:
- Predict labels for unlabeled traffic after fine-tuning.

Run from:

```bash
cd ET-BERT
```

Command:

```bash
python3 inference/run_classifier_infer.py \
  --load_model_path models/finetuned_model.bin \
  --vocab_path models/encryptd_vocab.txt \
  --test_path datasets/college_data/nolabel_test_dataset.tsv \
  --prediction_path datasets/college_data/prediction.tsv \
  --labels_num 1 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible
```

Expected result:

- `ET-BERT/datasets/college_data/prediction.tsv`

Important note:

- Set `--labels_num` to the true number of classes in the dataset.
- In the current `college_data` example, only class `0` is present, so `labels_num=1`.

### Stage 8: Export Embeddings for Comparison

Purpose:
- Convert ET-BERT model outputs into `.pkl` embedding files for intrinsic evaluation.

Run from:

```bash
cd demystifying-networks-main/src
```

#### Old Model Embeddings

```bash
python3 generate_embeddings.py \
  --pretrained_model_path ../../ET-BERT/models/pre-trained_model.bin \
  --vocab_path ../../ET-BERT/models/encryptd_vocab.txt \
  --config_path ../../ET-BERT/bert_base_config.json \
  --dataset_path ../../ET-BERT/datasets/college_data/test_dataset.tsv \
  --output_path ../embeddings/old_model/college_test_data_emb.pkl
```

#### New Model Embeddings

```bash
python3 generate_embeddings.py \
  --pretrained_model_path ../../ET-BERT/models/campus_pre-trained_model.bin-10 \
  --vocab_path ../../ET-BERT/models/encryptd_vocab.txt \
  --config_path ../../ET-BERT/bert_base_config.json \
  --dataset_path ../../ET-BERT/datasets/college_data/test_dataset.tsv \
  --output_path ../embeddings/new_model/college_test_data_emb.pkl
```

Expected result:

- `demystifying-networks-main/embeddings/old_model/*.pkl`
- `demystifying-networks-main/embeddings/new_model/*.pkl`

### Stage 9: Run the Intrinsic Evaluation

Purpose:
- Compare the old and new ET-BERT checkpoints using embedding-level evaluation.

Run from:

```bash
cd demystifying-networks-main/src
```

Command:

```bash
python3 run_evaluation.py \
  --old_model_embeddings_dir ../embeddings/old_model \
  --new_model_embeddings_dir ../embeddings/new_model \
  --vocab_path ../../ET-BERT/models/encryptd_vocab.txt \
  --config_path ../../ET-BERT/bert_base_config.json \
  --perturbation_dataset_path ../../ET-BERT/datasets/college_data/test_dataset.tsv \
  --old_model_path ../../ET-BERT/models/pre-trained_model.bin \
  --new_model_path ../../ET-BERT/models/campus_pre-trained_model.bin-10
```

Expected result:

- metrics printed in terminal
- summary blocks for cosine anisotropy, intrinsic dimensionality when available, perturbation sensitivity, and other enabled evaluations

## Local Workflow Summary

If you just want the shortest possible local path:

```bash
cd ET-BERT
python3 data_process/main.py --pcap_dir ../datasets/college_data/ --splitcap True
python3 data_process/main.py --pcap_dir ../datasets/college_data/splitcap/
python3 data_process/dataset_generation.py --pcap_dir ../datasets/college_data/splitcap/ --pretrain
python3 preprocess.py --corpus_path corpora/encrypted_burst.txt --vocab_path models/encryptd_vocab.txt --dataset_path dataset.pt --processes_num 20 --target bert
OMP_NUM_THREADS=8 python3 pre-training/pretrain.py --dataset_path dataset.pt --vocab_path models/encryptd_vocab.txt --output_model_path models/campus_pre-trained_model.bin --total_steps 10 --save_checkpoint_steps 10 --batch_size 4 --accumulation_steps 8 --instances_buffer_size 2560 --embedding word_pos_seg --encoder transformer --mask fully_visible --target bert
python3 finetuning/run_classifier.py --pretrained_model_path models/campus_pre-trained_model.bin-10 --vocab_path models/encryptd_vocab.txt --train_path datasets/college_data/train_dataset.tsv --dev_path datasets/college_data/valid_dataset.tsv --test_path datasets/college_data/test_dataset.tsv --epochs_num 10 --batch_size 32 --embedding word_pos_seg --encoder transformer --mask fully_visible --seq_length 128 --learning_rate 2e-5
python3 inference/run_classifier_infer.py --load_model_path models/finetuned_model.bin --vocab_path models/encryptd_vocab.txt --test_path datasets/college_data/nolabel_test_dataset.tsv --prediction_path datasets/college_data/prediction.tsv --labels_num 1 --embedding word_pos_seg --encoder transformer --mask fully_visible
```

## IITD HPC Workflow

This section is a practical starting point for IIT Delhi HPC use. Adjust usernames, scratch paths, GPU counts, and queue scripts to match your account.

### Basic HPC Setup

```bash
groups
echo $SCRATCH
module load python/3.10.4
export http_proxy=http://proxy61.iitd.ac.in:3128
export https_proxy=http://proxy61.iitd.ac.in:3128
```

Optional environment setup example:

```bash
module load apps/anaconda/3
conda create --prefix /scratch/<user>/col7560_work/conda_env python=3.10 openssl -y
conda activate /scratch/<user>/col7560_work/conda_env
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org scapy tqdm flowcontainer pandas numpy
```

### Dataset Processing on HPC

Run from:

```bash
cd /scratch/<user>/col7560/ET-BERT
```

Split raw PCAPs:

```bash
python3 data_process/main.py --pcap_dir /scratch/<user>/col7560/ET-BERT/datasets/college_data --splitcap True
```

Create labeled TSV datasets:

```bash
python3 data_process/main.py --pcap_dir /scratch/<user>/col7560/ET-BERT/datasets/college_data/splitcap/
```

Create pretraining corpus:

```bash
python3 data_process/dataset_generation.py --pcap_dir /scratch/<user>/col7560/ET-BERT/datasets/college_data/splitcap/ --pretrain
```

Create `dataset.pt`:

```bash
python3 preprocess.py \
  --corpus_path corpora/encrypted_burst.txt \
  --vocab_path models/encryptd_vocab.txt \
  --dataset_path dataset.pt \
  --processes_num 20 \
  --target bert
```

### Single-GPU HPC Pretraining

```bash
python3 pre-training/pretrain.py \
  --dataset_path dataset.pt \
  --vocab_path models/encryptd_vocab.txt \
  --output_model_path models/college_data_pre-trained_model.bin \
  --world_size 1 \
  --gpu_ranks 0 \
  --total_steps 500000 \
  --save_checkpoint_steps 10000 \
  --batch_size 32 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible \
  --target bert
```

Expected result:

- `models/college_data_pre-trained_model.bin-10000`
- later checkpoints at `-20000`, `-30000`, and so on

### Multi-GPU HPC Pretraining

```bash
python3 pre-training/pretrain.py \
  --dataset_path dataset.pt \
  --vocab_path models/encryptd_vocab.txt \
  --output_model_path models/pre-trained_model.bin \
  --world_size 8 \
  --gpu_ranks 0 1 2 3 4 5 6 7 \
  --total_steps 500000 \
  --save_checkpoint_steps 10000 \
  --batch_size 32 \
  --embedding word_pos_seg \
  --encoder transformer \
  --mask fully_visible \
  --target bert
```

Important note:

- Set `--world_size` and `--gpu_ranks` to match the GPUs actually allocated to your job.

### Fine-Tuning and Evaluation on HPC

The same fine-tuning, inference, embedding export, and evaluation commands can be reused on HPC by replacing the local relative dataset/model paths with absolute scratch paths.

## Output Checklist

After a successful full run, these are the main artifacts you should expect:

| Stage | Output |
|---|---|
| Split PCAPs | `ET-BERT/datasets/college_data/splitcap/...` |
| Labeled dataset | `train_dataset.tsv`, `valid_dataset.tsv`, `test_dataset.tsv`, `nolabel_test_dataset.tsv` |
| Dataset cache | `ET-BERT/datasets/college_data/dataset/*.npy` |
| Corpus | `ET-BERT/corpora/encrypted_burst.txt` |
| Pretraining dataset | `ET-BERT/dataset.pt` |
| Pretraining checkpoint | `ET-BERT/models/*.bin-<step>` |
| Fine-tuned classifier | `ET-BERT/models/finetuned_model.bin` |
| Inference output | `ET-BERT/datasets/college_data/prediction.tsv` |
| Embeddings | `demystifying-networks-main/embeddings/*/*.pkl` |

## Common Gotchas

- Use `data_process/dataset_generation.py --pretrain` to create `encrypted_burst.txt`.
- Use `finetuning/run_classifier.py`, not `fine-tuning/run_classifier.py`.
- The labeled TSV files are stored in `datasets/college_data/`, not inside `datasets/college_data/dataset/`.
- ET-BERT pretraining checkpoints are saved with step suffixes like `model.bin-10` or `model.bin-10000`.
- `labels_num` during inference must match the number of traffic classes.
- If the dataset only contains one class folder, such as only `0/`, classifier training will still run but it is not a meaningful multi-class experiment.

## Where to Look in the Code

- dataset splitting and TSV generation: `ET-BERT/data_process/main.py`
- burst corpus generation: `ET-BERT/data_process/dataset_generation.py`
- corpus preprocessing: `ET-BERT/preprocess.py`
- ET-BERT pretraining: `ET-BERT/pre-training/pretrain.py`
- fine-tuning: `ET-BERT/finetuning/run_classifier.py`
- inference: `ET-BERT/inference/run_classifier_infer.py`
- embedding export: `demystifying-networks-main/src/generate_embeddings.py`
- evaluation: `demystifying-networks-main/src/run_evaluation.py`

## Final Note

If you change dataset roots, model names, or scratch locations, update the paths consistently across all stages. Most failures in this project come from path mismatches, wrong working directory, or forgetting that some steps write outputs into the dataset root while others write into nested folders.

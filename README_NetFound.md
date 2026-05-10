# NetFound Intrinsic Evaluation — HPC Workflow Guide

This project combines two related codebases:

- `netFound/`: data preprocessing, tokenization, and embedding generation for network traffic
- `demystifying-networks/`: intrinsic evaluation framework for comparing network foundation models

This README is a practical handoff guide for teammates. It explains what each stage does, which directory to run commands from, what files should appear after each step, and how to run the full workflow on IITD HPC.

---

## Repository Layout

```text
/scratch/cse/phd/<my_my_entry_no>/
├── netFound/
│   ├── configs/
│   ├── data/
│   │   └── campus/
│   │       └── pretraining/
│   │           ├── raw/           ← symlinked .pcap files go here
│   │           ├── filtered/
│   │           ├── split/
│   │           ├── extracted/
│   │           └── final/
│   │               └── combined/  ← .arrow files ready for embedding
│   ├── pre_process_src/
│   ├── scripts/
│   └── run_campus_preprocess.sh
├── demystifying-networks/
│   └── src/
├── envs/netfound/                 ← conda environment (Python 3.10)
├── pcapplusplus/                  ← built PcapPlusPlus (bin/PcapSplitter etc.)
├── libpcap/                       ← built libpcap 1.10.4
├── PcapPlusPlus/                  ← PcapPlusPlus source
└── college_flows/                 ← raw campus .pcap files (~150GB)
```

---

## Working Rules

- Run preprocessing commands from `netFound/`.
- Run evaluation commands from `demystifying-networks/src/`.
- Raw pcap files must be symlinked (not copied) into `data/<dataset>/pretraining/raw/` to save disk space.
- Always load all modules and export environment variables at the start of every new session or PBS script.
- Use `source activate` instead of `conda activate` — conda was loaded via `module load`, not `conda init`.

---

## Required Tools

| Tool | Source |
|---|---|
| Python 3.10 | conda env at `/scratch/.../envs/netfound` |
| GNU parallel | `module load tools/parallel` |
| cmake 3.23+ | `module load apps/cmake/3.23.1/gnu` |
| PcapPlusPlus | Built from source (see Step 2) |
| libpcap 1.10.4 | Built from source (see Step 1) |
| GCC 9.1.0 | `/home/soft/centOS/compilers/gcc/9.1.0_new/bin/` |

---

## Session Setup (Run Every Time)

At the start of every new SSH session or at the top of every PBS script:

```bash
module load apps/anaconda/3EnvCreation
source activate /scratch/cse/phd/<my_my_entry_no>/envs/netfound
module load tools/parallel
module load apps/cmake/3.23.1/gnu

export PATH=/scratch/cse/phd/<my_entry_no>/pcapplusplus/bin:$PATH
export LD_LIBRARY_PATH=/home/apps/LIBISL/0.18/gnu/lib:/scratch/cse/phd/<my_entry_no>/libpcap/lib:/home/soft/centOS/compilers/gcc/9.1.0_new/lib64:$LD_LIBRARY_PATH
unset PYTHONPATH
```

---

## One-Time Setup (Do Once)

### Step 1 — Build libpcap from Source

The system libpcap at `/usr/lib64/libpcap.so.1.5.3` exists but lacks development headers. Build from source into your scratch space:

```bash
cd /scratch/cse/phd/<my_entry_no>/

wget https://www.tcpdump.org/release/libpcap-1.10.4.tar.gz
tar xzf libpcap-1.10.4.tar.gz
cd libpcap-1.10.4

./configure --prefix=/scratch/cse/phd/<my_entry_no>/libpcap
make -j8
make install
```

Success check:
```bash
ls /scratch/cse/phd/<my_entry_no>/libpcap/lib/libpcap.so
ls /scratch/cse/phd/<my_entry_no>/libpcap/include/pcap.h
```

---

### Step 2 — Build PcapPlusPlus from Source

PcapPlusPlus provides the `1_filter` and `PcapSplitter` binaries required by the preprocessing pipeline.

```bash
module load apps/cmake/3.23.1/gnu
export LD_LIBRARY_PATH=/home/apps/LIBISL/0.18/gnu/lib:$LD_LIBRARY_PATH

cd /scratch/cse/phd/<my_entry_no>/

# HTTPS is blocked on HPC — use HTTP
git clone http://github.com/seladb/PcapPlusPlus.git
cd PcapPlusPlus

# Remove benchmark example (it tries to clone from GitHub during build)
rm -rf Examples/PcapPlusPlus-benchmark
sed -i 's/add_subdirectory(PcapPlusPlus-benchmark)/#add_subdirectory(PcapPlusPlus-benchmark)/' Examples/CMakeLists.txt

cmake -S . -B build \
    -DCMAKE_INSTALL_PREFIX=/scratch/cse/phd/<my_entry_no>/pcapplusplus \
    -DPCAP_ROOT=/scratch/cse/phd/<my_entry_no>/libpcap \
    -DCMAKE_C_COMPILER=/home/soft/centOS/compilers/gcc/9.1.0_new/bin/gcc \
    -DCMAKE_CXX_COMPILER=/home/soft/centOS/compilers/gcc/9.1.0_new/bin/g++ \
    -DCMAKE_CXX_STANDARD=17 \
    -DPCAPPP_BUILD_EXAMPLES=ON \
    -DPCAPPP_BUILD_BENCHMARKS=OFF

cmake --build build -j8
cmake --install build
```

Success check:
```bash
ls /scratch/cse/phd/<my_entry_no>/pcapplusplus/bin/PcapSplitter
```

---

### Step 3 — Build NetFound Preprocessing Binaries

NetFound's preprocessing pipeline requires two compiled C++ binaries: `1_filter` and `3_field_extraction`.

```bash
cd /scratch/cse/phd/<my_entry_no>/netFound

cmake -S pre_process_src/packets_processing_src -B build/preprocess \
    -DCMAKE_PREFIX_PATH="/scratch/cse/phd/<my_entry_no>/pcapplusplus;/scratch/cse/phd/<my_entry_no>/libpcap" \
    -DPCAP_ROOT=/scratch/cse/phd/<my_entry_no>/libpcap \
    -DCMAKE_C_COMPILER=/home/soft/centOS/compilers/gcc/9.1.0_new/bin/gcc \
    -DCMAKE_CXX_COMPILER=/home/soft/centOS/compilers/gcc/9.1.0_new/bin/g++ \
    -DCMAKE_CXX_STANDARD=17

cmake --build build/preprocess -j8

cp build/preprocess/1_filter pre_process_src/
cp build/preprocess/3_field_extraction pre_process_src/
```

Success check:
```bash
ls -la /scratch/cse/phd/<my_entry_no>/netFound/pre_process_src/1_filter
ls -la /scratch/cse/phd/<my_entry_no>/netFound/pre_process_src/3_field_extraction
```

---

### Step 4 — Clone the Evaluation Framework

```bash
cd /scratch/cse/phd/<my_entry_no>/

# HTTPS is blocked on HPC — use HTTP
git clone http://github.com/maybe-hello-world/demystifying-networks.git

pip install -r demystifying-networks/requirements.txt
```

Success check:
```bash
ls /scratch/cse/phd/<my_entry_no>/demystifying-networks/src/
```

---

## End-to-End Workflow

### Stage 1 — Prepare Raw Data

NetFound expects pcap files inside a `raw/` subfolder of the input folder. Symlink (do not copy) to save disk space:

```bash
mkdir -p /scratch/cse/phd/<my_entry_no>/netFound/data/campus/pretraining/raw

# Symlink files using find (glob fails with 150GB+ directories)
find /scratch/cse/phd/<my_entry_no>/college_flows/ -name "*.pcap" | head -2000 | \
    xargs -I{} ln -s {} /scratch/cse/phd/<my_entry_no>/netFound/data/campus/pretraining/raw/
```

Success check:
```bash
ls /scratch/cse/phd/<my_entry_no>/netFound/data/campus/pretraining/raw/ | wc -l
```

Important notes:
- Each `.pcap` file in `college_flows/` is a single network flow captured from campus traffic.
- Pass the **parent** of `raw/` as `--input_folder`, not `raw/` itself. The pipeline appends `/raw` internally.
- Do not use `ls *.pcap` with large directories — use `find` to avoid "Argument list too long" errors.

---

### Stage 2 — Run Preprocessing Pipeline (PBS Job)

The preprocessing runs four steps internally: filter → split → extract → tokenize.

Create the job script:

```bash
cat > /scratch/cse/phd/<my_entry_no>/netFound/run_campus_preprocess.sh << 'EOF'
#!/bin/bash
#PBS -N campus_preprocess
#PBS -P col7560.course
#PBS -q standard
#PBS -m bea
#PBS -M <my_entry_no>@iitd.ac.in
#PBS -l select=1:ncpus=20:centos=haswell
#PBS -l walltime=04:00:00
#PBS -l software=PYTHON

cd /scratch/cse/phd/<my_entry_no>/netFound

module load apps/anaconda/3EnvCreation
source activate /scratch/cse/phd/<my_entry_no>/envs/netfound
module load tools/parallel

export PATH=/scratch/cse/phd/<my_entry_no>/pcapplusplus/bin:$PATH
export LD_LIBRARY_PATH=/home/apps/LIBISL/0.18/gnu/lib:/scratch/cse/phd/<my_entry_no>/libpcap/lib:/home/soft/centOS/compilers/gcc/9.1.0_new/lib64:$LD_LIBRARY_PATH
unset PYTHONPATH

python3 scripts/preprocess_data.py \
    --input_folder data/campus/pretraining \
    --action pretrain \
    --tokenizer_config configs/DefaultConfigNoTCPOptions.json \
    --combined
EOF
```

Submit:
```bash
qsub /scratch/cse/phd/<my_entry_no>/netFound/run_campus_preprocess.sh
```

Monitor:
```bash
qstat -answ -u <my_entry_no>
```

Check output on completion:
```bash
cat /scratch/cse/phd/<my_entry_no>/netFound/campus_preprocess.o<jobid>
cat /scratch/cse/phd/<my_entry_no>/netFound/campus_preprocess.e<jobid>
```

Expected results:
- `data/campus/pretraining/filtered/` — filtered pcap files
- `data/campus/pretraining/split/` — per-connection pcap files
- `data/campus/pretraining/extracted/` — extracted packet fields
- `data/campus/pretraining/final/shards/` — tokenized Arrow shards
- `data/campus/pretraining/final/combined/*.arrow` — combined Arrow file ready for embedding

Success check:
```bash
ls /scratch/cse/phd/<my_entry_no>/netFound/data/campus/pretraining/final/combined/
```

---

### Stage 3 — Generate Embeddings

Feed the preprocessed campus data through the frozen pretrained NetFound checkpoint (2.7GB, 663M parameters):

```bash
export NETFOUND=/scratch/cse/phd/<my_entry_no>/netFound

nohup /scratch/cse/phd/<my_entry_no>/envs/netfound/bin/python3 \
    $NETFOUND/run_causal_sensitivity.py \
    > $NETFOUND/causal.log 2>&1 &

echo "PID: $!"
tail -f $NETFOUND/causal.log
```

Important notes:
- Allow ~2 minutes for model load before the progress bar appears.
- A FutureWarning about the `device` argument from transformers is harmless.
- If running on CPU only (no GPU allocated), reduce `LIMIT` to avoid multi-hour runtimes:
  ```bash
  sed -i 's/LIMIT = 5000/LIMIT = 500/' $NETFOUND/run_causal_sensitivity.py
  ```
- With `LIMIT = 500` on CPU, expect ~1 hour runtime.

---

### Stage 4 — Run Intrinsic Evaluation

The evaluation framework applies three complementary analyses to the generated embeddings:

| Analysis | What it measures |
|---|---|
| **Embedding Geometry** | How well NetFound utilises its representation space (anisotropy, isotropy) |
| **Metric Alignment** | Correlation between embeddings and expert features (flow duration, packet sizes, TCP dynamics) |
| **Causal Sensitivity** | How embeddings shift when protocol fields are perturbed (TTL, port number, packet size, etc.) |

Run from:
```bash
cd /scratch/cse/phd/<my_entry_no>/demystifying-networks/src/
```

Each experiment is a Jupyter notebook. To run as a script on HPC without Jupyter:
```bash
jupyter nbconvert --to script <notebook>.ipynb
python3 <notebook>.py
```

---

## Output Checklist

After a successful full run, the main artifacts should be:

| Stage | Output |
|---|---|
| Data prep | `data/campus/pretraining/raw/*.pcap` (symlinks) |
| Filter | `data/campus/pretraining/filtered/*.pcap` |
| Split | `data/campus/pretraining/split/*/` |
| Extract | `data/campus/pretraining/extracted/*/` |
| Tokenize | `data/campus/pretraining/final/shards/` |
| Combine | `data/campus/pretraining/final/combined/*.arrow` |
| Embeddings | `$NETFOUND/embeddings.npy` or equivalent |
| Evaluation | printed metrics and plots from notebooks |

---

## Common Issues and Fixes

| Problem | Fix |
|---|---|
| `conda activate` not working | Use `source activate` instead |
| `cmake: command not found` | `module load apps/cmake/3.23.1/gnu` |
| `parallel: command not found` | `module load tools/parallel` |
| `Input folder raw does not exist` | Pass the **parent** of `raw/` as `--input_folder` |
| `1_filter` exit status 127 | Add `LD_LIBRARY_PATH` for libpcap and GCC 9.1 libs; add `parallel` to PATH |
| `PcapSplitter: command not found` | `export PATH=/scratch/.../pcapplusplus/bin:$PATH` |
| `libisl.so.15` not found | `export LD_LIBRARY_PATH=/home/apps/LIBISL/0.18/gnu/lib:$LD_LIBRARY_PATH` |
| GitHub `git clone` fails (HTTPS) | Use `git clone http://github.com/...` instead of `https://` |
| Wrong Python used in subprocess | `unset PYTHONPATH` before running scripts |
| GPU job stuck: Insufficient ngpus | Reduce `LIMIT` and run on CPU, or wait for GPU allocation |
| `ls *.pcap`: Argument list too long | Use `find <dir> -name "*.pcap"` instead of glob |
| `TestPretrainingConfig.json` not found | Use `configs/DefaultConfigNoTCPOptions.json` |
| `du` / `ls` hangs on large directory | Use `find` with `head` to limit output |

---

## Where to Look in the Code

- Preprocessing entry point: `netFound/scripts/preprocess_data.py`
- Filter step: `netFound/pre_process_src/1_filter.sh` → calls `1_filter` binary
- Split step: `netFound/pre_process_src/2_pcap_splitting.sh` → calls `PcapSplitter`
- Extract step: `netFound/pre_process_src/3_extract_fields.sh` → calls `3_field_extraction` binary
- Tokenize step: `netFound/pre_process_src/Tokenize.py`
- Combine step: `netFound/pre_process_src/CollectTokensInFiles.py`
- Geometry analysis: `demystifying-networks/src/geometry*.ipynb`
- Metric alignment: `demystifying-networks/src/alignment*.ipynb`
- Causal sensitivity: `demystifying-networks/src/perturb*.ipynb`

---

## Final Note

If you change dataset roots, scratch locations, or my_entry_no, update paths consistently across all PBS scripts and export statements. Most failures in this project come from three things: missing `LD_LIBRARY_PATH` for the custom-built libs, `PYTHONPATH` pollution causing the wrong Python in subprocesses, or passing `raw/` directly as `--input_folder` instead of its parent.

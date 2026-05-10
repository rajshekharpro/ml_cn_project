# run_yatc_eval.py
import sys
sys.path.append("/mnt/bigdisk/mayank/netmamba_pretrain/YaTC")

import numpy as np
import pandas as pd
import torch
import copy
from pathlib import Path
from tqdm.auto import tqdm
from finetune import build_dataset
import models_YaTC
from util.pos_embed import interpolate_pos_embed
from intrinsic_evaluation import IntrinsicEvaluationFramework, DatasetInput

# ── HARDCODE THESE ────────────────────────────────────────────────────────────
DATA_PATH   = "/mnt/bigdisk/mayank/netmamba_pretrain/datas/"   # folder containing train/ with your .png files
MODEL_PATH  = "/mnt/bigdisk/mayank/netmamba_pretrain/YaTC/output_dir_512-36k/checkpoint-step10000.pth"
DATASET_NAME = "ISCXVPN"
BATCH_SIZE  = 128
LIMIT       = 100_000   # max samples, set to float('inf') for all
N_CLASSES   = 5         # ISCXVPN=7, ISCXTor=8, USTC=20, CICIoT=10
CIC_CSV_PATH = "/mnt/bigdisk/mayank/netmamba_pretrain/demystifying-networks/src/cicflow_features_aligned.csv"

# ─────────────────────────────────────────────────────────────────────────────

NON_FEATURE_COLUMNS = {
    "Flow ID",
    "Source IP",
    "Source Port",
    "Destination IP",
    "Destination Port",
    "Src IP",
    "Src Port",
    "Dst IP",
    "Dst Port",
    "Protocol",
    "Timestamp",
    "Label",
    "filename",
    "source_key",
    "source_file",
}


def get_loader(data_path, batch_size):
    args = lambda: None
    args.data_path = data_path
    dataset = build_dataset(is_train=True, args=args)
    return torch.utils.data.DataLoader(
        dataset,
        sampler=torch.utils.data.SequentialSampler(dataset),
        batch_size=batch_size,
        num_workers=4,
        pin_memory=False,
        drop_last=True,
    )


def get_model(model_path, n_classes):
    checkpoint_model = torch.load(model_path)['model']
    model = models_YaTC.__dict__['TraFormer_YaTC'](
        num_classes=n_classes,
        drop_path_rate=0.1,
    )
    interpolate_pos_embed(model, checkpoint_model)

    checkpoint_model['fc_norm.bias']   = checkpoint_model['norm.bias']
    checkpoint_model['fc_norm.weight'] = checkpoint_model['norm.weight']

    keys_to_del = ['mask_token', 'norm.weight', 'norm.bias']
    for key in list(checkpoint_model.keys()):
        if key.startswith('decoder'):
            keys_to_del.append(key)
    for key in keys_to_del:
        del checkpoint_model[key]

    model.load_state_dict(checkpoint_model, strict=False)
    model.eval()
    model.to("cuda")
    return model


def encode(batch, model):
    batch = batch.to("cuda")
    with torch.no_grad():
        return model.forward_features(batch).cpu()   # (B, 192) — no .mean(dim=1)


def get_embeddings(loader, model, limit):
    all_emb = []
    total = 0
    for batch, labels in tqdm(loader, desc="Encoding"):
        emb = encode(batch, model)

        if total == 0:
            print(f"Embedding shape per batch: {emb.shape}")   # should be (B, 192)
            print(f"Embedding std: {emb.std().item():.4f}")     # should be > 0.01

        all_emb.append(emb.numpy())
        total += emb.shape[0]
        if total >= limit:
            break

    embeddings = np.concatenate(all_emb, axis=0)
    print(f"Total embeddings: {embeddings.shape}")
    return embeddings


def _image_source_key(image_path):
    path = Path(image_path)
    try:
        return str(path.relative_to(DATA_PATH).with_suffix(""))
    except ValueError:
        return str(Path(*path.parts[-3:]).with_suffix(""))


def load_cic_features(csv_path, image_keys=None):
    cic_df = pd.read_csv(csv_path)
    cic_df.columns = [col.strip() for col in cic_df.columns]

    feature_cols = [
        col for col in cic_df.columns
        if col not in NON_FEATURE_COLUMNS and pd.api.types.is_numeric_dtype(cic_df[col])
    ]
    if not feature_cols:
        raise ValueError(f"No numeric CICFlowMeter feature columns found in {csv_path}")

    keep_mask = None
    if image_keys is not None and "source_key" in cic_df.columns:
        cic_df["source_key"] = cic_df["source_key"].astype(str)
        grouped = cic_df.groupby("source_key", sort=False)[feature_cols].mean()
        aligned = grouped.reindex(image_keys)
        keep_mask = ~aligned.isna().all(axis=1).to_numpy()
        missing = int((~keep_mask).sum())
        if missing:
            print(f"Warning: {missing} embeddings have no matching CIC row and will be skipped.")
        cic_features = (
            aligned.loc[keep_mask, feature_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
            .to_numpy(dtype=np.float32)
        )
        print(f"Aligned CIC features by source_key: {cic_features.shape} from {csv_path}")
        return cic_features, feature_cols, keep_mask

    if image_keys is not None:
        print("Warning: CIC CSV has no source_key column; falling back to row-order alignment.")

    cic_features = (
        cic_df[feature_cols]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )
    print(f"Loaded CIC features: {cic_features.shape} from {csv_path}")
    return cic_features, feature_cols, keep_mask


if __name__ == "__main__":
    print("Loading model...")
    model = get_model(MODEL_PATH, N_CLASSES)

    print("Loading data...")
    loader = get_loader(DATA_PATH, BATCH_SIZE)

    print("Extracting embeddings...")
    embeddings = get_embeddings(loader, model, LIMIT)
    image_keys = [_image_source_key(path) for path, _ in loader.dataset.samples[:embeddings.shape[0]]]

    # Quick sanity check before evaluation
    assert embeddings.ndim == 2,          f"Expected 2D array, got {embeddings.shape}"
    assert embeddings.shape[1] == 192,    f"Expected 192 dims, got {embeddings.shape[1]}"
    assert embeddings.std() > 0.01,       "Embeddings look collapsed (std too low)"

    print("Loading CICFlowMeter features...")
    cic_features, cic_feature_names, keep_mask = load_cic_features(CIC_CSV_PATH, image_keys=image_keys)
    if keep_mask is not None:
        embeddings = embeddings[keep_mask]
    if cic_features.shape[0] != embeddings.shape[0]:
        n = min(cic_features.shape[0], embeddings.shape[0])
        print(
            "Warning: embeddings and CIC feature counts differ; "
            f"using first {n} rows "
            f"(embeddings={embeddings.shape[0]}, cic_features={cic_features.shape[0]})."
        )
        embeddings = embeddings[:n]
        cic_features = cic_features[:n]

    datasets = [
        DatasetInput(
            dataset_name=DATASET_NAME,
            embeddings=embeddings,
            cic_embeddings=embeddings,
            cic_features=cic_features,
            cic_feature_names=cic_feature_names,
        )
    ]

    try:
        from dadapy.data import Data
        has_dadapy = True
    except ImportError:
        has_dadapy = False
        print("dadapy not found — skipping intrinsic dimensionality")

    framework = IntrinsicEvaluationFramework(
        compute_anisotropy=True,
        compute_intrinsic_dim=has_dadapy,
        compute_cka_cic=True,
        compute_causal_sensitivity=False, # set True if you have synthetic traffic data
        compute_synth_math=False,
        verbose=True,
    )

    results = framework.evaluate(datasets)
    print(results.summary())

    # Save results
    with open(f"{DATASET_NAME}_eval_results.txt", "w") as f:
        f.write(results.summary())
    print(f"Saved to {DATASET_NAME}_eval_results.txt")

    if results.cka_with_cic_features and DATASET_NAME in results.cka_with_cic_features:
        cka_df = (
            pd.Series(results.cka_with_cic_features[DATASET_NAME], name="cka")
            .sort_values(ascending=False)
            .rename_axis("feature")
            .reset_index()
        )
        out_path = f"{DATASET_NAME}_cka_features.csv"
        cka_df.to_csv(out_path, index=False)
        print(f"Saved per-feature CKA to {out_path}")
        print("Top CKA features:")
        print(cka_df.head(10).to_string(index=False))

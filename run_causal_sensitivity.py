import sys
sys.path.insert(0, '/scratch/cse/phd/csz258235/netFound/src')
sys.path.insert(0, '/scratch/cse/phd/csz258235/netFound/src/modules')

import torch
import numpy as np
from tqdm import tqdm
from datasets import load_dataset
from netFoundModels import netFoundBaseModel
from netFoundConfigs import netFoundLarge
from netFoundTokenizer import netFoundTokenizer
from netFoundDataCollator import SimpleDataCollator
import json

DATA_PATH = "/scratch/cse/phd/csz258235/netFound/data/campus/pretraining/final/combined"
MODEL_PATH = "/scratch/cse/phd/csz258235/netFound/checkpoints/netFound-640M-base"
BATCH_SIZE = 32
LIMIT = 50

print("Loading model...")
config = netFoundLarge()
config.pretraining = True
model = netFoundBaseModel.from_pretrained(
    MODEL_PATH, config=config, ignore_mismatched_sizes=True
)
model.eval()
print("Model loaded!")

print("Loading data...")
dataset = load_dataset("arrow", data_dir=DATA_PATH, split="train",
                       streaming=False, cache_dir="/dev/shm/cachetmp")

total_bursts = [0] * len(dataset)
dataset = dataset.add_column("total_bursts", total_bursts)

tokenizer = netFoundTokenizer(config=config)
tokenizer.raw_labels = True

def preprocess_function(examples):
    return tokenizer(examples)

print("Tokenizing...")
dataset = dataset.map(preprocess_function, batched=True, num_proc=10, load_from_cache_file=True)

dataloader = torch.utils.data.DataLoader(
    dataset.remove_columns(['burst_tokens', 'directions', 'counts']),
    batch_size=BATCH_SIZE,
    num_workers=4,
    collate_fn=SimpleDataCollator(config.max_burst_length),
    drop_last=True,
)

def encode(batch, model):
    with torch.no_grad():
        batch['position_ids'] = torch.arange(
            batch['input_ids'].size(1)
        ).unsqueeze(0).expand(batch['input_ids'].size(0), -1)
        # dataset_burst_sizes: number of bursts per flow
        if 'dataset_burst_sizes' in batch:
            burst_sizes = batch['dataset_burst_sizes']
        else:
            # estimate from attention mask
            burst_sizes = torch.sum(batch['attention_mask'], dim=1).int()
        output = model(
            input_ids=batch['input_ids'],
            attention_mask=batch['attention_mask'],
            position_ids=batch['position_ids'],
            direction=batch['direction'],
            iats=batch['iats'],
            bytes=batch['bytes'],
            return_dict=True,
            pkt_count=batch.get("pkt_count", None),
            protocol=batch['protocol'],
            dataset_burst_sizes=burst_sizes,
        ).last_hidden_state
        return torch.mean(output, 1).cpu(), batch["labels"]

def repeat_mask(packet_mask):
    burst_mask = packet_mask.repeat(1, 6)
    burst_mask = torch.cat((torch.tensor([0]).unsqueeze(0), burst_mask), dim=1)
    flow_mask = burst_mask.repeat(1, 12)
    return flow_mask

def get_embeddings(dataloader, model, flow_mask, random=False):
    embeddings = []
    total = 0
    for batch in tqdm(dataloader):
        orig_input = batch['input_ids'].clone()
        if not random:
            noise = batch['input_ids'].clone()
            for col in range(batch['input_ids'].size(1)):
                perm = torch.randperm(batch['input_ids'].size(0))
                noise[:, col] = batch['input_ids'][perm, col]
        else:
            noise = torch.randint(0, 65535, size=batch['input_ids'].size())
        mask = flow_mask.repeat(batch['input_ids'].size(0), 1)
        mask = mask[:, :batch['input_ids'].size(1)].bool()
        batch['input_ids'][mask] = noise[mask]
        emb, _ = encode(batch, model)
        embeddings.append(emb)
        total += batch['input_ids'].size(0)
        if total >= LIMIT:
            break
    return torch.cat(embeddings)

def calculate_similarity(original, perturbed):
    return torch.nn.functional.cosine_similarity(perturbed, original).mean().item()

def perturb_and_compare(name, packet_mask, original_emb):
    mask = repeat_mask(packet_mask.unsqueeze(0))
    print(f"\n--- {name} ---")
    rand_emb = get_embeddings(dataloader, model, mask, random=True)
    n = min(len(original_emb), len(rand_emb))
    sim_rand = calculate_similarity(original_emb[:n], rand_emb[:n])
    print(f"Cosine sim (random):  {sim_rand:.4f}")
    reord_emb = get_embeddings(dataloader, model, mask, random=False)
    n = min(len(original_emb), len(reord_emb))
    sim_reord = calculate_similarity(original_emb[:n], reord_emb[:n])
    print(f"Cosine sim (reorder): {sim_reord:.4f}")
    return sim_rand, sim_reord

print("\nGenerating original embeddings...")
zero_mask = torch.tensor([0] * 18)
original_emb = get_embeddings(dataloader, model, repeat_mask(zero_mask.unsqueeze(0)))
print(f"Original embeddings shape: {original_emb.shape}")

results = {}
results['payload']       = perturb_and_compare('Payload',          torch.tensor([0]*12 + [1]*6), original_emb)
results['seq_ack']       = perturb_and_compare('SEQ/ACK',          torch.tensor([0]*7 + [1]*4 + [0]*7), original_emb)
results['ip_length']     = perturb_and_compare('IP Total Length',   torch.tensor([0]*2 + [1]*1 + [0]*15), original_emb)
results['ip_ttl']        = perturb_and_compare('IP TTL',            torch.tensor([0]*4 + [1]*1 + [0]*13), original_emb)
results['tcp_flags']     = perturb_and_compare('TCP Flags',         torch.tensor([0]*5 + [1]*1 + [0]*12), original_emb)
results['tcp_wsize']     = perturb_and_compare('TCP Window Size',   torch.tensor([0]*6 + [1]*1 + [0]*11), original_emb)

print("\n\n========== CAUSAL SENSITIVITY RESULTS ==========")
print(f"{'Field':<25} {'Random':>10} {'Reorder':>10}")
print("-" * 47)
for field, (rand, reord) in results.items():
    print(f"{field:<25} {rand:>10.4f} {reord:>10.4f}")
print("=================================================")
print("(Lower cosine similarity = higher sensitivity)")

with open('/scratch/cse/phd/csz258235/netFound/causal_sensitivity_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print("\nResults saved!")

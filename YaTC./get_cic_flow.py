"""
pcap_to_cicflow.py
──────────────────
Converts a directory of PCAP files into a CICFlowMeter-compatible CSV.

Uses scapy for packet parsing and computes the same ~80 features that
CICFlowMeter exports, so the CSV can be fed directly to the
`compute_cka_cic=True` branch of IntrinsicEvaluationFramework.

Requirements:
    pip install scapy pandas numpy tqdm

Usage:
    python pcap_to_cicflow.py \
        --pcap_dir  /data/pcaps/ \
        --out_csv   /data/cicflow_features.csv \
        --label_map '{"vpn_browsing":0,"vpn_streaming":1}' \  # optional
        --workers   8
"""

import argparse
import json
import math
import multiprocessing as mp
import os
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ── optional: faster packet parsing ──────────────────────────────────────────
try:
    from scapy.all import PcapReader, IP, IPv6, TCP, UDP, ICMP
    HAS_SCAPY = True
except ImportError:
    HAS_SCAPY = False
    raise ImportError("scapy is required: pip install scapy")


# ─────────────────────────────────────────────────────────────────────────────
# Flow key helpers
# ─────────────────────────────────────────────────────────────────────────────

def _flow_key(pkt):
    """Return a canonical 5-tuple (src, dst, sport, dport, proto)."""
    if IP in pkt:
        src, dst = pkt[IP].src, pkt[IP].dst
        proto    = pkt[IP].proto
    elif IPv6 in pkt:
        src, dst = pkt[IPv6].src, pkt[IPv6].dst
        proto    = pkt[IPv6].nh
    else:
        return None

    sport = dport = 0
    if TCP in pkt:
        sport, dport = pkt[TCP].sport, pkt[TCP].dport
    elif UDP in pkt:
        sport, dport = pkt[UDP].sport, pkt[UDP].dport

    # Canonical direction: smaller (ip, port) first
    fwd = (src, sport) <= (dst, dport)
    if fwd:
        return (src, dst, sport, dport, proto)
    else:
        return (dst, src, dport, sport, proto)


# ─────────────────────────────────────────────────────────────────────────────
# Per-flow accumulator
# ─────────────────────────────────────────────────────────────────────────────

class FlowRecord:
    __slots__ = [
        "src", "dst", "sport", "dport", "proto",
        "label",
        # packet lists per direction
        "fwd_pkts", "bwd_pkts",          # payload lengths
        "fwd_times", "bwd_times",         # timestamps
        "fwd_header", "bwd_header",       # header lengths
        "fwd_flags", "bwd_flags",         # TCP flag bytes
        "fwd_iat", "bwd_iat",             # inter-arrival times
        "all_times",
        "start_time", "end_time",
        "fin_cnt", "syn_cnt", "rst_cnt",
        "psh_cnt", "ack_cnt", "urg_cnt",
        "fwd_psh", "bwd_psh",
        "fwd_urg", "bwd_urg",
        "init_fwd_win", "init_bwd_win",
        "act_data_fwd",                   # fwd pkts with payload > 0
    ]

    def __init__(self, key, label):
        self.src, self.dst, self.sport, self.dport, self.proto = key
        self.label = label
        self.fwd_pkts    = []; self.bwd_pkts    = []
        self.fwd_times   = []; self.bwd_times   = []
        self.fwd_header  = []; self.bwd_header  = []
        self.fwd_flags   = []; self.bwd_flags   = []
        self.fwd_iat     = []; self.bwd_iat     = []
        self.all_times   = []
        self.start_time  = None
        self.end_time    = None
        self.fin_cnt = self.syn_cnt = self.rst_cnt = 0
        self.psh_cnt = self.ack_cnt = self.urg_cnt = 0
        self.fwd_psh = self.bwd_psh = 0
        self.fwd_urg = self.bwd_urg = 0
        self.init_fwd_win = self.init_bwd_win = -1
        self.act_data_fwd = 0

    def add_packet(self, pkt, ts, is_fwd):
        self.all_times.append(ts)
        if self.start_time is None:
            self.start_time = ts
        self.end_time = ts

        # payload length (IP total - IP header - transport header)
        payload = 0
        hdr_len = 0
        if IP in pkt:
            ip_hdr  = pkt[IP].ihl * 4
            ip_tot  = pkt[IP].len
        elif IPv6 in pkt:
            ip_hdr  = 40
            ip_tot  = pkt[IPv6].plen + 40
        else:
            return

        if TCP in pkt:
            tcp_hdr  = pkt[TCP].dataofs * 4
            hdr_len  = ip_hdr + tcp_hdr
            payload  = max(0, ip_tot - hdr_len)
            flags    = int(pkt[TCP].flags)
            win      = pkt[TCP].window
            self.fin_cnt += bool(flags & 0x01)
            self.syn_cnt += bool(flags & 0x02)
            self.rst_cnt += bool(flags & 0x04)
            self.psh_cnt += bool(flags & 0x08)
            self.ack_cnt += bool(flags & 0x10)
            self.urg_cnt += bool(flags & 0x20)
            if is_fwd:
                self.fwd_flags.append(flags)
                self.fwd_psh += bool(flags & 0x08)
                self.fwd_urg += bool(flags & 0x20)
                if self.init_fwd_win == -1:
                    self.init_fwd_win = win
            else:
                self.bwd_flags.append(flags)
                self.bwd_psh += bool(flags & 0x08)
                self.bwd_urg += bool(flags & 0x20)
                if self.init_bwd_win == -1:
                    self.init_bwd_win = win
        elif UDP in pkt:
            hdr_len = ip_hdr + 8
            payload = max(0, ip_tot - hdr_len)

        if is_fwd:
            prev = self.fwd_times[-1] if self.fwd_times else None
            self.fwd_pkts.append(payload)
            self.fwd_times.append(ts)
            self.fwd_header.append(hdr_len)
            if payload > 0:
                self.act_data_fwd += 1
            if prev is not None:
                self.fwd_iat.append(ts - prev)
        else:
            prev = self.bwd_times[-1] if self.bwd_times else None
            self.bwd_pkts.append(payload)
            self.bwd_times.append(ts)
            self.bwd_header.append(hdr_len)
            if prev is not None:
                self.bwd_iat.append(ts - prev)


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction (mirrors CICFlowMeter column names exactly)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_div(a, b, fallback=0.0):
    return a / b if b else fallback

def _stats(arr):
    """Return (mean, std, max, min) for a list; 0 if empty."""
    if not arr:
        return 0.0, 0.0, 0.0, 0.0
    a = np.array(arr, dtype=np.float64)
    return float(a.mean()), float(a.std()), float(a.max()), float(a.min())

def _flow_to_row(rec: FlowRecord) -> dict:
    r = rec
    duration = (r.end_time - r.start_time) if r.start_time != r.end_time else 1e-6
    duration_us = duration * 1e6   # CIC uses microseconds for rates

    fwd_pkt_count = len(r.fwd_pkts)
    bwd_pkt_count = len(r.bwd_pkts)
    total_pkts    = fwd_pkt_count + bwd_pkt_count

    fwd_bytes = sum(r.fwd_pkts)
    bwd_bytes = sum(r.bwd_pkts)
    total_bytes = fwd_bytes + bwd_bytes

    fwd_m, fwd_s, fwd_mx, fwd_mn = _stats(r.fwd_pkts)
    bwd_m, bwd_s, bwd_mx, bwd_mn = _stats(r.bwd_pkts)
    all_bytes = r.fwd_pkts + r.bwd_pkts
    all_m, all_s, all_mx, all_mn = _stats(all_bytes)

    fwd_iat_m, fwd_iat_s, fwd_iat_mx, fwd_iat_mn = _stats(r.fwd_iat)
    bwd_iat_m, bwd_iat_s, bwd_iat_mx, bwd_iat_mn = _stats(r.bwd_iat)

    all_iat = []
    if len(r.all_times) > 1:
        t = sorted(r.all_times)
        all_iat = [t[i+1]-t[i] for i in range(len(t)-1)]
    flow_iat_m, flow_iat_s, flow_iat_mx, flow_iat_mn = _stats(all_iat)

    fwd_hdr_m, _, _, _ = _stats(r.fwd_header)
    bwd_hdr_m, _, _, _ = _stats(r.bwd_header)

    # Active/Idle — simplified (single active segment)
    act_mean = act_std = act_max = act_min = 0.0
    idle_mean = idle_std = idle_max = idle_min = 0.0

    row = {
        # identifiers
        " Source IP":           r.src,
        " Source Port":         r.sport,
        " Destination IP":      r.dst,
        " Destination Port":    r.dport,
        " Protocol":            r.proto,
        " Timestamp":           r.start_time,
        # duration
        " Flow Duration":       int(duration_us),
        # packet counts
        " Total Fwd Packets":   fwd_pkt_count,
        " Total Backward Packets": bwd_pkt_count,
        # byte totals
        "Total Length of Fwd Packets": fwd_bytes,
        " Total Length of Bwd Packets": bwd_bytes,
        # fwd pkt length stats
        " Fwd Packet Length Max": fwd_mx,
        " Fwd Packet Length Min": fwd_mn,
        " Fwd Packet Length Mean": fwd_m,
        " Fwd Packet Length Std":  fwd_s,
        # bwd pkt length stats
        "Bwd Packet Length Max":  bwd_mx,
        " Bwd Packet Length Min": bwd_mn,
        " Bwd Packet Length Mean": bwd_m,
        " Bwd Packet Length Std":  bwd_s,
        # rates
        "Flow Bytes/s":   _safe_div(total_bytes, duration),
        " Flow Packets/s": _safe_div(total_pkts, duration),
        # flow IAT
        " Flow IAT Mean": flow_iat_m * 1e6,
        " Flow IAT Std":  flow_iat_s * 1e6,
        " Flow IAT Max":  flow_iat_mx * 1e6,
        " Flow IAT Min":  flow_iat_mn * 1e6,
        # fwd IAT
        "Fwd IAT Total": sum(r.fwd_iat) * 1e6,
        " Fwd IAT Mean":  fwd_iat_m * 1e6,
        " Fwd IAT Std":   fwd_iat_s * 1e6,
        " Fwd IAT Max":   fwd_iat_mx * 1e6,
        " Fwd IAT Min":   fwd_iat_mn * 1e6,
        # bwd IAT
        "Bwd IAT Total": sum(r.bwd_iat) * 1e6,
        " Bwd IAT Mean":  bwd_iat_m * 1e6,
        " Bwd IAT Std":   bwd_iat_s * 1e6,
        " Bwd IAT Max":   bwd_iat_mx * 1e6,
        " Bwd IAT Min":   bwd_iat_mn * 1e6,
        # push / urgent flags
        "Fwd PSH Flags": r.fwd_psh,
        " Bwd PSH Flags": r.bwd_psh,
        " Fwd URG Flags": r.fwd_urg,
        " Bwd URG Flags": r.bwd_urg,
        # header lengths
        " Fwd Header Length":   sum(r.fwd_header),
        " Bwd Header Length":   sum(r.bwd_header),
        # packet rates
        "Fwd Packets/s": _safe_div(fwd_pkt_count, duration),
        " Bwd Packets/s": _safe_div(bwd_pkt_count, duration),
        # all-packet stats
        " Min Packet Length": all_mn,
        " Max Packet Length": all_mx,
        " Packet Length Mean": all_m,
        " Packet Length Std":  all_s,
        " Packet Length Variance": all_s ** 2,
        # TCP flags aggregate
        "FIN Flag Count": r.fin_cnt,
        " SYN Flag Count": r.syn_cnt,
        " RST Flag Count": r.rst_cnt,
        " PSH Flag Count": r.psh_cnt,
        " ACK Flag Count": r.ack_cnt,
        " URG Flag Count": r.urg_cnt,
        " CWE Flag Count": 0,
        " ECE Flag Count": 0,
        # ratios
        " Down/Up Ratio": _safe_div(bwd_bytes, fwd_bytes),
        " Average Packet Size": _safe_div(total_bytes, total_pkts),
        " Avg Fwd Segment Size": fwd_m,
        " Avg Bwd Segment Size": bwd_m,
        " Fwd Header Length.1": sum(r.fwd_header),   # CIC duplicate column
        # bulk features (simplified)
        "Fwd Avg Bytes/Bulk":   0,
        " Fwd Avg Packets/Bulk": 0,
        " Fwd Avg Bulk Rate":    0,
        " Bwd Avg Bytes/Bulk":   0,
        " Bwd Avg Packets/Bulk": 0,
        "Bwd Avg Bulk Rate":     0,
        # subflow
        "Subflow Fwd Packets": fwd_pkt_count,
        " Subflow Fwd Bytes":   fwd_bytes,
        " Subflow Bwd Packets": bwd_pkt_count,
        " Subflow Bwd Bytes":   bwd_bytes,
        # TCP window init
        "Init_Win_bytes_forward":  r.init_fwd_win,
        " Init_Win_bytes_backward": r.init_bwd_win,
        " act_data_pkt_fwd":        r.act_data_fwd,
        " min_seg_size_forward":    min(r.fwd_header) if r.fwd_header else 0,
        # active / idle
        "Active Mean": act_mean,
        " Active Std":  act_std,
        " Active Max":  act_max,
        " Active Min":  act_min,
        "Idle Mean": idle_mean,
        " Idle Std":  idle_std,
        " Idle Max":  idle_max,
        " Idle Min":  idle_min,
        # label
        " Label": r.label,
    }
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Per-PCAP worker
# ─────────────────────────────────────────────────────────────────────────────

FLOW_TIMEOUT   = 120.0   # seconds — hard timeout per flow
ACTIVE_TIMEOUT =  60.0   # seconds — idle gap that ends active segment

def process_pcap(args):
    pcap_path, label, flow_timeout, pcap_root = args
    flows   = {}     # key -> FlowRecord
    rows    = []

    try:
        reader = PcapReader(str(pcap_path))
        for pkt in reader:
            ts  = float(pkt.time)
            key = _flow_key(pkt)
            if key is None:
                continue

            if key not in flows:
                flows[key] = FlowRecord(key, label)

            rec = flows[key]

            # Timeout check: export and reset if stale
            if rec.start_time is not None and (ts - rec.start_time) > flow_timeout:
                rows.append(_flow_to_row(rec))
                flows[key] = FlowRecord(key, label)
                rec = flows[key]

            # Determine direction: fwd = (src, sport) matches key's first two fields
            src_pkt, sport_pkt = None, None
            if IP in pkt:
                src_pkt = pkt[IP].src
            elif IPv6 in pkt:
                src_pkt = pkt[IPv6].src
            if TCP in pkt:
                sport_pkt = pkt[TCP].sport
            elif UDP in pkt:
                sport_pkt = pkt[UDP].sport

            is_fwd = (src_pkt == key[0]) and (sport_pkt == key[2])
            rec.add_packet(pkt, ts, is_fwd)

            # TCP FIN/RST → export immediately
            if TCP in pkt and (int(pkt[TCP].flags) & 0x05):
                rows.append(_flow_to_row(rec))
                del flows[key]

        # Export remaining open flows
        for rec in flows.values():
            rows.append(_flow_to_row(rec))

    except Exception as e:
        print(f"[WARN] {pcap_path.name}: {e}")

    try:
        source_key = str(pcap_path.relative_to(pcap_root).with_suffix(""))
    except ValueError:
        source_key = str(pcap_path.with_suffix(""))

    for row in rows:
        row["source_key"] = source_key
        row["source_file"] = str(pcap_path)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Directory scanner
# ─────────────────────────────────────────────────────────────────────────────

def discover_pcaps(pcap_dir: Path, label_map: Optional[dict]):
    """
    Recursively find .pcap / .pcapng files.
    Label assignment strategy (in order of priority):
      1. label_map  {filename_stem -> label_str}  — explicit mapping
      2. Parent folder name                        — common dataset layout
      3. 'unknown'
    """
    entries = []
    for ext in ("*.pcap", "*.pcapng", "*.cap"):
        for p in sorted(pcap_dir.rglob(ext)):
            if label_map:
                label = label_map.get(p.stem, label_map.get(p.parent.name, "unknown"))
            else:
                label = p.parent.name   # e.g.  vpn_browsing / vpn_streaming
            entries.append((p, label))
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="PCAP → CICFlowMeter CSV")
    ap.add_argument("--pcap_dir",     required=True,  type=Path,
                    help="Root folder to search for .pcap/.pcapng files")
    ap.add_argument("--out_csv",      required=True,  type=Path,
                    help="Output CSV path")
    ap.add_argument("--label_map",    default=None,   type=str,
                    help='JSON string: {"stem_or_folder":"label",...}')
    ap.add_argument("--workers",      default=max(1, mp.cpu_count() - 1), type=int,
                    help="Parallel worker processes (default: nCPU-1)")
    ap.add_argument("--flow_timeout", default=120.0,  type=float,
                    help="Flow hard timeout in seconds (default: 120)")
    ap.add_argument("--chunk_size",   default=4,      type=int,
                    help="PCAPs per worker task (default: 4)")
    ap.add_argument("--one_row_per_pcap", action="store_true",
                    help="Keep one dominant flow row per PCAP for one-embedding-per-PCAP evaluations")
    args = ap.parse_args()

    label_map = json.loads(args.label_map) if args.label_map else None
    entries   = discover_pcaps(args.pcap_dir, label_map)

    if not entries:
        print(f"No PCAP files found under {args.pcap_dir}")
        return

    print(f"Found {len(entries)} PCAP(s) → processing with {args.workers} worker(s)")

    tasks = [(p, lbl, args.flow_timeout, args.pcap_dir) for p, lbl in entries]

    all_rows = []
    with mp.Pool(processes=args.workers) as pool:
        for rows in tqdm(
            pool.imap_unordered(process_pcap, tasks, chunksize=args.chunk_size),
            total=len(tasks),
            desc="Processing PCAPs",
        ):
            all_rows.extend(rows)

    if not all_rows:
        print("No flows extracted — check PCAP validity.")
        return

    df = pd.DataFrame(all_rows)

    # Drop flows with zero packets in both directions (degenerate)
    df = df[df[" Total Fwd Packets"] + df[" Total Backward Packets"] > 0].copy()

    if args.one_row_per_pcap:
        before_collapse = len(df)
        if "source_key" not in df.columns:
            raise ValueError("Cannot collapse to one row per PCAP because source_key is missing.")
        df["_packet_count_for_sort"] = df[" Total Fwd Packets"] + df[" Total Backward Packets"]
        df.sort_values(
            ["source_key", "_packet_count_for_sort", " Flow Duration"],
            ascending=[True, False, False],
            inplace=True,
        )
        df = df.drop_duplicates("source_key", keep="first").drop(columns=["_packet_count_for_sort"])
        print(f"Collapsed {before_collapse:,} extracted flow row(s) to {len(df):,} PCAP row(s).")

    # Replace inf / NaN (division by zero edge cases)
    df.replace([np.inf, -np.inf], 0.0, inplace=True)
    df.fillna(0.0, inplace=True)

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out_csv, index=False)

    print(f"\n✓  Saved {len(df):,} flows → {args.out_csv}")
    print(f"   Label distribution:\n{df[' Label'].value_counts().to_string()}")


if __name__ == "__main__":
    main()

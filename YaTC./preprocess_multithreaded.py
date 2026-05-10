import os
import argparse
import binascii
import numpy as np
import multiprocessing as mp
from PIL import Image
import time
from tqdm import tqdm

def raw_packet_to_string(packet):
    """YaTC logic: 160 hex chars header, 480 hex chars payload"""
    try:
        # Extract IP Layer
        if 'IP' in packet:
            ip_layer = packet['IP']
            header = binascii.hexlify(bytes(ip_layer)).decode()
        else:
            header = '0' * 160
            
        # Extract Raw Payload
        if 'Raw' in packet:
            payload = binascii.hexlify(bytes(packet['Raw'])).decode()
            header = header.replace(payload, '') # Clean header
        else:
            payload = ''
    except Exception:
        header, payload = '0' * 160, '0' * 480

    # Truncate/Pad to YaTC spec
    header = header[:160].ljust(160, '0')
    payload = payload[:480].ljust(480, '0')
    return header, payload

def process_one(task):
    """Worker function: Processes one pcap into one 40x40 PNG"""
    pcap_path, out_png_path = task
    
    if os.path.exists(out_png_path):
        return True, None

    try:
        import scapy.all as scapy
        # EFFICIENCY BOOST: Use PcapReader instead of rdpcap
        # This streams packets instead of loading the whole file
        data = []
        with scapy.PcapReader(pcap_path) as reader:
            for i, pkt in enumerate(reader):
                if i >= 5: break
                h, p = raw_packet_to_string(pkt)
                data.append(h + p)
        
        # Padding flows with less than 5 packets
        while len(data) < 5:
            data.append('0' * 160 + '0' * 480)

        flow_string = ''.join(data)
        # Convert hex string to uint8 array
        content = np.frombuffer(binascii.unhexlify(flow_string), dtype=np.uint8)
        
        # Save as 40x40 Grayscale
        img = Image.fromarray(content.reshape(40, 40), mode='L')
        img.save(out_png_path, format='PNG', compress_level=1)
        return True, None
    except Exception as e:
        return False, f"Error {pcap_path}: {str(e)}"


def main():
    parser = argparse.ArgumentParser(description="YaTC High-Speed Preprocessing")
    parser.add_argument('--input_dir', type=str, required=True, help='Path: subset/train')
    parser.add_argument('--output_dir', type=str, required=True, help='Path: out/train')
    parser.add_argument('--workers', type=int, default=os.cpu_count(), help='Workers')
    parser.add_argument('--chunk_size', type=int, default=128, help='Chunk size')
    
    args = parser.parse_args()

    # 1. Scan for files
    # Clean paths to remove trailing slashes which mess up relpath
    input_root = os.path.abspath(args.input_dir)
    output_root = os.path.abspath(args.output_dir)


    input_dirs = [
        os.path.join(input_root, d) for d in os.listdir(input_root)
        if os.path.isdir(os.path.join(input_root, d))
        ]

    output_dirs =[
        os.path.join(output_root, d) for d in os.listdir(input_root)
        ]
    #print(output_dirs,zip(input_dirs,output_dirs))
    

    for in_shard, out_shard in zip(input_dirs,output_dirs):
        os.makedirs(out_shard, exist_ok=True)

        print(f"Scanning {in_shard}...")
        pcap_files = []
        for root, _, files in os.walk(in_shard):
            for f in files:
                if f.endswith('.pcap'):
                    pcap_files.append(os.path.join(root, f))
        
        print(f"Found {len(pcap_files):,} pcaps.")

        # 2. Build tasks with strict Directory Mirroring
        tasks = []
        for path in pcap_files:
            # This gets 'shard_000/file.pcap' relative to 'subset/train'
            rel_path = os.path.relpath(path, in_shard)
            
            # This creates 'out/train/shard_000/file.png'
            target_path = os.path.join(out_shard, rel_path).replace('.pcap', '.png')
            
            # Pre-create the shard directory in the output location
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            tasks.append((path, target_path))

        # 3. Parallel Execution
        print(f"Starting {args.workers} workers...")
        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=args.workers) as pool:
            for ok, err in tqdm(pool.imap_unordered(process_one, tasks, chunksize=args.chunk_size), total=len(tasks)):
                if not ok:
                    with open("failed_log.txt", "a") as f:
                        f.write(f"{err}\n")

if __name__ == '__main__':
    main()

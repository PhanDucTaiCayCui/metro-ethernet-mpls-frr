#!/usr/bin/env python3
"""
=============================================================================
TOOL.PY - ĐO LƯỜNG HIỆU NĂNG MẠNG METRO ETHERNET MPLS (CLI-ONLY)
=============================================================================
Không cần GUI / Tkinter. Chạy thẳng từ terminal, lưu kết quả ra PNG + CSV.

Yêu cầu:
  - topology.py đang chạy (Mininet đang mở CLI)
  - sudo python3 tool.py                → Chạy tất cả 5 case
  - sudo python3 tool.py --cases 1,2,3  → Chỉ chạy case chọn
  - sudo python3 tool.py --case 1       → Chỉ chạy case 1

Kết quả lưu tại: ~/Desktop/CK/results/
  case1_ping_matrix.png + .csv
  case2_mpls_traceroute.png + .csv
  case3_throughput.png + .csv
  case4_delay_jitter.png + .csv
  case5_stress_test.png + .csv
=============================================================================
"""

import os
import re
import sys
import csv
import time
import datetime

import matplotlib
matplotlib.use('Agg')          # Không cần display / Xserver
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

# Thư mục kết quả: tạo thư mục results/ cạnh file tool.py (không hardcode đường dẫn)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_SCRIPT_DIR, 'results')
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, 'tool_report.log')

IP_MAP = {
    'h1': '10.1.0.1', 'h2': '10.1.0.2', 'h3': '10.1.0.3', 'h4': '10.1.0.4',
    'admin1': '10.2.10.1', 'admin2': '10.2.10.2',
    'lab1':   '10.2.20.1', 'lab2':   '10.2.20.2',
    'guest1': '10.2.30.1', 'guest2': '10.2.30.2',
    'web1': '10.3.10.1', 'web2': '10.3.10.2',
    'dns1': '10.3.20.1', 'dns2': '10.3.20.2',
    'db1':  '10.3.30.1', 'db2':  '10.3.30.2',
}


# ═══════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def log(msg: str):
    """In ra console và ghi vào log file."""
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def get_ip(node: str) -> str:
    return IP_MAP.get(node, node)


def ns(node: str, cmd: str) -> str:
    """
    Chạy lệnh trong network namespace của Mininet node.
    Sử dụng ip netns exec (thông qua symlink) hoặc nsenter (thông qua PID).
    Capture cả stderr để phát hiện namespace bị stale.
    """
    import subprocess
    link = f'/var/run/netns/{node}'

    if os.path.islink(link) and os.path.exists(link):   # islink + exists = symlink target still live
        try:
            full_cmd = f'ip netns exec {node} {cmd}'
            r = subprocess.run(
                full_cmd, shell=True,
                capture_output=True, text=True, timeout=30
            )
            return (r.stdout or '').strip()
        except Exception:
            pass

    # Fallback: nsenter thông qua /proc/<pid>/ns/net (bắt buộc symlink phải chỉ đúng)
    if os.path.islink(link):
        target = os.readlink(link)          # /proc/<pid>/ns/net
        if os.path.exists(target):
            try:
                r = subprocess.run(
                    f'nsenter --net={target} -- {cmd}',
                    shell=True, capture_output=True, text=True, timeout=30
                )
                return (r.stdout or '').strip()
            except Exception:
                pass

    return ''


def check_netns() -> bool:
    """
    Kiểm tra namespace có thực sự hoạt động không bằng cách chạy lệnh thử.
    os.path.islink() chỉ check file tồn tại, KHÔNG verify target namespace còn sống.
    """
    link = '/var/run/netns/h1'

    # Bước 1: symlink có tồn tại không?
    if not os.path.islink(link):
        print('\n⚠  LỖI: Không tìm thấy /var/run/netns/h1 (symlink không tồn tại)')
        print('   → Hãy chạy topology.py TRƯỚC rồi mới chạy tool.py trong terminal khác.')
        print('   → Lệnh: sudo python3 ~/Desktop/CK/topology.py\n')
        return False

    # Bước 2: target có còn sống không? (stale check)
    target = os.readlink(link)
    if not os.path.exists(target):
        print(f'\n⚠  LỖI STALE NAMESPACE: Symlink tồn tại nhưng target đã chết ({target})')
        print('   → Mininet đã bị dừng. Hãy restart topology.py rồi chạy lại tool.py.')
        print('   → Lệnh cleanup: sudo mn -c && sudo python3 ~/Desktop/CK/topology.py\n')
        return False

    # Bước 3: thử chạy lệnh thực sự trong namespace
    test_out = ns('h1', 'ip addr show lo')
    if not test_out:
        print('\n⚠  LỖI NAMESPACE: Symlink tồn tại nhưng không thể execute trong namespace h1.')
        print('   → Thử: sudo ip netns exec h1 ip addr show lo')
        print('   → Nếu lỗi, restart: sudo mn -c && sudo python3 ~/Desktop/CK/topology.py\n')
        return False

    print(f'  ✔ Namespace h1 OK (target: {target})')
    return True



# ═══════════════════════════════════════════════════════════════════════════
# MEASUREMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def measure_ping(src: str, dst: str, count: int = 5) -> dict:
    """Đo RTT, Jitter, Loss bằng ping. Trả về dict kết quả."""
    dst_ip = get_ip(dst)
    out = ns(src, f'ping -c {count} -W 2 -q {dst_ip}')

    result = dict(src=src, dst=dst, dst_ip=dst_ip,
                  rtt_min=-1.0, rtt_avg=-1.0, rtt_max=-1.0,
                  jitter=-1.0, loss_pct=100.0, raw=out)

    m = re.search(r'(\d+)%\s+packet\s+loss', out)
    if m:
        result['loss_pct'] = float(m.group(1))

    m2 = re.search(r'min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
    if m2:
        result.update(rtt_min=float(m2.group(1)), rtt_avg=float(m2.group(2)),
                      rtt_max=float(m2.group(3)), jitter=float(m2.group(4)))
    return result


def measure_throughput(src: str, dst: str, duration: int = 5) -> dict:
    """
    Đo throughput bằng iperf3/iperf.
    Dùng & background thay vì -D (daemonize không hoạt động trong ip netns exec).
    """
    dst_ip = get_ip(dst)

    # Dừng server/client cũ còn sót
    ns(dst, 'killall -9 iperf3 iperf 2>/dev/null')
    ns(src, 'killall -9 iperf3 iperf 2>/dev/null')
    time.sleep(0.5)

    # Phát hiện công cụ khả dụng
    use_iperf3 = ns(dst, 'which iperf3') != ''
    use_iperf  = ns(dst, 'which iperf')  != ''

    out = ''
    if use_iperf3:
        # Khởi động server iperf3 ở background trong netns của dst
        srv_cmd = f'ip netns exec {dst} iperf3 -s --one-off -p 5201 > /dev/null 2>&1 &'
        os.system(srv_cmd)
        time.sleep(1.0)   # Chờ server sẵn sàng
        out = ns(src, f'iperf3 -c {dst_ip} -p 5201 -t {duration} -f m 2>/dev/null')
    elif use_iperf:
        srv_cmd = f'ip netns exec {dst} iperf -s -p 5001 > /dev/null 2>&1 &'
        os.system(srv_cmd)
        time.sleep(1.0)
        out = ns(src, f'iperf -c {dst_ip} -p 5001 -t {duration} -f m 2>/dev/null')

    mbps = 0.0
    m = re.search(r'([\d.]+)\s*Mbits?/sec', out, re.I)
    if m:
        mbps = float(m.group(1))

    # Dọn dẹp server
    ns(dst, 'killall -9 iperf3 iperf 2>/dev/null')
    return dict(src=src, dst=dst, dst_ip=dst_ip, mbps=mbps, raw=out)


def measure_tracepath(src: str, dst: str, max_ttl: int = 12) -> dict:
    """
    Mô phỏng traceroute bằng cách tăng TTL từng bước (ping -t TTL).
    Phù hợp với mạng MPLS/static routing không trả ICMP TTL-exceeded đầy đủ.
    """
    dst_ip = get_ip(dst)
    hops = []
    seen = set()

    for ttl in range(1, max_ttl + 1):
        # Gửi 1 ping với TTL cố định, bắt IP của node trả lời
        out = ns(src, f'ping -c 1 -W 1 -t {ttl} {dst_ip} 2>&1')

        # Nếu đến đích rồi
        if 'bytes from' in out and ('icmp_seq' in out or 'icmp_req' in out):
            if dst_ip not in seen:
                hops.append(dst_ip)
            break

        # Lấy IP của router trả ICMP Time Exceeded
        m = re.search(r'From ([\d.]+).*[Tt]ime.*exceeded', out)
        if not m:
            # Thử dạng khác: "From X: icmp_seq=1 Time to live exceeded"
            m = re.search(r'From ([\d.]+)', out)
        if m:
            ip = m.group(1)
            if ip not in seen:
                seen.add(ip)
                hops.append(ip)

    # Fallback: nếu không bắt được hop nào nhưng đích vẫn reachable
    if not hops:
        chk = ns(src, f'ping -c 1 -W 2 {dst_ip}')
        if '0% packet loss' in chk or '1 received' in chk:
            hops = [dst_ip]

    path_str = ' → '.join(dict.fromkeys(hops)) if hops else 'UNREACHABLE'
    return dict(src=src, dst=dst, dst_ip=dst_ip,
                hop_count=len(hops), path_str=path_str, raw='')


# ═══════════════════════════════════════════════════════════════════════════
# CASE 1: PING MATRIX
# ═══════════════════════════════════════════════════════════════════════════

def case1_ping_matrix():
    log('\n' + '═'*55)
    log('CASE 1: Ma trận Ping liên chi nhánh (RTT & Loss)')
    log('═'*55)

    reps = [('h1', 'B1-Flat'), ('admin1', 'B2-3Layer'), ('web1', 'B3-Leaf')]
    N = len(reps)
    rtt_mat  = np.full((N, N), np.nan)
    loss_mat = np.zeros((N, N))
    raw_rows = []

    for i, (src, sl) in enumerate(reps):
        for j, (dst, dl) in enumerate(reps):
            if i == j:
                rtt_mat[i, j] = 0.0
                raw_rows.append((src, dst, 0, 0, 0, 0))
                continue
            log(f'  Ping {src:8s} → {dst:8s} ...')
            r = measure_ping(src, dst, count=5)
            rtt_mat[i, j]  = r['rtt_avg'] if r['rtt_avg'] >= 0 else np.nan
            loss_mat[i, j] = r['loss_pct']
            raw_rows.append((src, dst, r['rtt_min'], r['rtt_avg'], r['rtt_max'], r['loss_pct']))
            ok = '✓' if r['loss_pct'] == 0 else '✗'
            log(f'    [{ok}] RTT={r["rtt_avg"]:.2f}ms  Loss={r["loss_pct"]:.0f}%')

    labels = [lbl for _, lbl in reps]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle('CASE 1: Ma trận Ping liên chi nhánh\nMetro Ethernet MPLS', fontweight='bold', fontsize=13)

    def draw_heatmap(ax, data, title, fmt, cmap):
        d = np.where(np.isnan(data), 0, data)
        im = ax.imshow(d, cmap=cmap, aspect='auto')
        ax.set_xticks(range(N)); ax.set_yticks(range(N))
        ax.set_xticklabels(labels, rotation=15, ha='right', fontsize=10)
        ax.set_yticklabels(labels, fontsize=10)
        ax.set_title(title, fontweight='bold')
        plt.colorbar(im, ax=ax)
        for ii in range(N):
            for jj in range(N):
                v = data[ii, jj]
                txt = fmt(v) if not np.isnan(v) else 'N/A'
                ax.text(jj, ii, txt, ha='center', va='center', fontweight='bold', fontsize=11)

    draw_heatmap(ax1, rtt_mat,  'RTT Trung bình (ms)', lambda v: f'{v:.1f}', 'YlOrRd')
    draw_heatmap(ax2, loss_mat, 'Tỉ lệ mất gói (%)',  lambda v: f'{v:.0f}%', 'Reds')

    fig.tight_layout()
    img = os.path.join(LOG_DIR, 'case1_ping_matrix.png')
    fig.savefig(img, dpi=150, bbox_inches='tight'); plt.close(fig)

    with open(img.replace('.png', '.csv'), 'w', newline='') as f:
        csv.writer(f).writerows([['Src','Dst','RTT_Min','RTT_Avg','RTT_Max','Loss%']] + raw_rows)

    log(f'  → Lưu: {img}')
    return img


# ═══════════════════════════════════════════════════════════════════════════
# CASE 2: TRACEPATH / PATH VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def case2_path_trace():
    log('\n' + '═'*55)
    log('CASE 2: Kiểm tra đường đi qua MPLS Backbone')
    log('═'*55)

    pairs = [
        ('h1',     'web1',   'B1(Flat) → B3(Leaf-Spine)'),
        ('admin1', 'db1',    'B2(3-Layer) → B3(Leaf-Spine)'),
        ('h1',     'admin1', 'B1(Flat) → B2(3-Layer)'),
        ('web1',   'h1',     'B3(Leaf-Spine) → B1(Flat)'),
    ]

    rows = []
    for src, dst, desc in pairs:
        log(f'  Tracepath {src} → {dst} ...')
        r = measure_tracepath(src, dst)
        ok = '✓' if r['hop_count'] > 0 else '✗'
        log(f'    [{ok}] {r["hop_count"]} hops: {r["path_str"][:70]}')
        rows.append((desc, src, dst, r['hop_count'], r['path_str']))

    # ── Thu thập MPLS forwarding table từ các router backbone ─────────────
    log('  Thu thập MPLS label table từ PE/P routers...')
    mpls_nodes = [
        ('pe1', 'PE1 (B1 edge)'),
        ('pe2', 'PE2 (B2 edge)'),
        ('pe3', 'PE3 (B3 edge)'),
        ('p1',  'P1  (core)'),
        ('p2',  'P2  (core)'),
        ('p3',  'P3  (core)'),
        ('p4',  'P4  (core)'),
    ]
    mpls_rows = []
    for node, label in mpls_nodes:
        is_pe = node.startswith('pe')
        if is_pe:
            # PE: push entries nằm trong IP routing table với 'encap mpls'
            raw = ns(node, 'ip route show')
            entries = [ln.strip() for ln in raw.split('\n') if 'encap mpls' in ln]
        else:
            # P: swap/pop entries nằm trong MPLS forwarding table
            raw = ns(node, 'ip -f mpls route show')
            entries = [ln.strip() for ln in raw.split('\n') if ln.strip()]

        if entries:
            for e in entries[:5]:   # max 5 dòng mỗi router
                mpls_rows.append((label, e))
            log(f'    {label}: {len(entries)} MPLS entries')
        else:
            mpls_rows.append((label, '(no MPLS entries — topology chưa cấu hình?)'))
            log(f'    {label}: NO MPLS entries')


    # ── Hình 1: Bảng đường đi ─────────────────────────────────────────────
    n_path = len(rows)
    n_mpls = max(len(mpls_rows), 1)
    fig, (ax1, ax2) = plt.subplots(2, 1,
                                    figsize=(15, 4 + n_path * 1.2 + n_mpls * 0.5),
                                    gridspec_kw={'height_ratios': [n_path + 1, n_mpls + 1]})

    # Bảng 1: Path trace
    ax1.axis('off')
    ax1.set_title('CASE 2A: Đường đi gói tin qua MPLS Backbone\n'
                  '(Xác nhận kết nối liên chi nhánh)',
                  fontweight='bold', fontsize=12, pad=12)
    tbl1 = ax1.table(
        cellText=rows,
        colLabels=['Kịch bản', 'Src', 'Dst', 'Hops', 'Path (IP hops)'],
        cellLoc='left', loc='center',
        colColours=['#2a9d8f'] * 5,
    )
    tbl1.auto_set_font_size(False); tbl1.set_fontsize(8); tbl1.scale(1, 2.5)
    for (r, c), cell in tbl1.get_celld().items():
        cell.set_edgecolor('#cccccc')
        if r == 0:
            cell.set_text_props(color='white', fontweight='bold')

    # Bảng 2: MPLS label forwarding
    ax2.axis('off')
    ax2.set_title('CASE 2B: MPLS Label Forwarding Table (ip -f mpls route show)\n'
                  '→ Xác nhận Label Switching đang hoạt động trên backbone',
                  fontweight='bold', fontsize=11, pad=10)
    if mpls_rows:
        tbl2 = ax2.table(
            cellText=mpls_rows,
            colLabels=['Router', 'MPLS Route Entry'],
            cellLoc='left', loc='center',
            colColours=['#457b9d', '#457b9d'],
        )
        tbl2.auto_set_font_size(False); tbl2.set_fontsize(8); tbl2.scale(1, 2.2)
        for (r, c), cell in tbl2.get_celld().items():
            cell.set_edgecolor('#dee2e6')
            if r == 0:
                cell.set_text_props(color='white', fontweight='bold')
            elif r % 2 == 0:
                cell.set_facecolor('#f8f9fa')

    fig.tight_layout(pad=1.5)
    img = os.path.join(LOG_DIR, 'case2_path_trace.png')
    fig.savefig(img, dpi=150, bbox_inches='tight'); plt.close(fig)

    with open(img.replace('.png', '.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerows([['=== PATH TRACE ===']])
        w.writerows([['Desc', 'Src', 'Dst', 'Hops', 'Path']] + rows)
        w.writerows([[]])
        w.writerows([['=== MPLS LABEL TABLE ===']])
        w.writerows([['Router', 'MPLS_Route']] + mpls_rows)

    log(f'  → Lưu: {img}')
    return img



# ═══════════════════════════════════════════════════════════════════════════
# CASE 3: THROUGHPUT
# ═══════════════════════════════════════════════════════════════════════════

def case3_throughput():
    log('\n' + '═'*55)
    log('CASE 3: So sánh Throughput (iperf) giữa các chi nhánh')
    log('═'*55)

    tests = [
        ('h1',     'h2',     'B1↔B1\n(Intra Flat)',       '#2a9d8f'),
        ('admin1', 'admin2', 'B2↔B2\n(Intra 3-Layer)',    '#e9c46a'),
        ('web1',   'db1',    'B3↔B3\n(Intra Leaf-Spine)', '#e76f51'),
        ('h1',     'admin1', 'B1↔B2\n(Inter)',             '#457b9d'),
        ('h1',     'web1',   'B1↔B3\n(Inter)',             '#a8dadc'),
        ('admin1', 'db1',    'B2↔B3\n(Inter)',             '#f4a261'),
    ]

    labels, mbps_list, colors, raw_rows = [], [], [], []

    for src, dst, label, color in tests:
        log(f'  iperf {src:8s} → {dst:8s} ...')
        r = measure_throughput(src, dst, duration=5)
        labels.append(label)
        mbps_list.append(r['mbps'])
        colors.append(color)
        raw_rows.append((src, dst, label.replace('\n', ' '), r['mbps']))
        log(f'    {r["mbps"]:.2f} Mbps')

    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(labels))
    bars = ax.bar(x, mbps_list, color=colors, edgecolor='white', linewidth=1.5, width=0.6)

    for bar, val in zip(bars, mbps_list):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                    f'{val:.1f}', ha='center', va='bottom', fontweight='bold', fontsize=10)

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel('Throughput (Mbps)')
    ax.set_title('CASE 3: So sánh Throughput giữa các chi nhánh\n'
                 '(Đo bằng iperf - 5s/kịch bản)', fontweight='bold', fontsize=12)
    ax.grid(axis='y', alpha=0.35, linestyle='--')

    patches = [
        mpatches.Patch(color='#2a9d8f', label='Intra B1 (Flat)'),
        mpatches.Patch(color='#e9c46a', label='Intra B2 (3-Layer)'),
        mpatches.Patch(color='#e76f51', label='Intra B3 (Leaf-Spine)'),
        mpatches.Patch(color='#457b9d', label='Inter B1↔B2'),
        mpatches.Patch(color='#a8dadc', label='Inter B1↔B3'),
        mpatches.Patch(color='#f4a261', label='Inter B2↔B3'),
    ]
    ax.legend(handles=patches, fontsize=9)
    fig.tight_layout()

    img = os.path.join(LOG_DIR, 'case3_throughput.png')
    fig.savefig(img, dpi=150, bbox_inches='tight'); plt.close(fig)

    with open(img.replace('.png', '.csv'), 'w', newline='') as f:
        csv.writer(f).writerows([['Src','Dst','Scenario','Mbps']] + raw_rows)

    log(f'  → Lưu: {img}')
    return img


# ═══════════════════════════════════════════════════════════════════════════
# CASE 4: DELAY / JITTER / LOSS
# ═══════════════════════════════════════════════════════════════════════════

def case4_delay_jitter():
    log('\n' + '═'*55)
    log('CASE 4: Delay / Jitter / Packet Loss - So sánh 3 kiến trúc')
    log('═'*55)

    targets = [
        ('h1',     'h2',     'B1 Intra\n(Flat)'),
        ('admin1', 'lab1',   'B2 Intra\n(3-Layer)'),
        ('web1',   'db1',    'B3 Intra\n(Leaf-Spine)'),
        ('h1',     'admin1', 'B1→B2\n(Inter)'),
        ('h1',     'web1',   'B1→B3\n(Inter)'),
        ('admin1', 'db1',    'B2→B3\n(Inter)'),
    ]

    lbls, rtts, jits, loss_vals, raw_rows = [], [], [], [], []

    for src, dst, lbl in targets:
        log(f'  Ping {src:8s} → {dst:8s} (10 gói)...')
        r = measure_ping(src, dst, count=10)
        rtt = max(r['rtt_avg'],  0)
        jit = max(r['jitter'],   0)
        los = r['loss_pct']
        lbls.append(lbl)
        rtts.append(rtt)
        jits.append(jit)
        loss_vals.append(los)
        raw_rows.append((src, dst, lbl.replace('\n', ' '), rtt, jit, los))
        log(f'    RTT={rtt:.2f}ms  Jitter={jit:.2f}ms  Loss={los:.0f}%')

    n = len(targets)
    x = np.arange(n)
    w = 0.26

    fig, ax = plt.subplots(figsize=(14, 6))
    b1 = ax.bar(x - w, rtts,      w, label='RTT Avg (ms)',    color='#2a9d8f', edgecolor='white')
    b2 = ax.bar(x,     jits,      w, label='Jitter (ms)',     color='#e9c46a', edgecolor='white')
    b3 = ax.bar(x + w, loss_vals, w, label='Packet Loss (%)', color='#e76f51', edgecolor='white')

    for bars in (b1, b2, b3):
        for bar in bars:
            h = bar.get_height()
            if h > 0.05:
                ax.text(bar.get_x() + bar.get_width() / 2, h + 0.05,
                        f'{h:.1f}', ha='center', va='bottom', fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels(lbls, fontsize=9)
    ax.set_ylabel('Giá trị (ms / %)')
    ax.set_title('CASE 4: So sánh Delay / Jitter / Packet Loss\n'
                 'Giữa 3 kiến trúc LAN: Flat | 3-Layer | Leaf-Spine',
                 fontweight='bold', fontsize=12)
    ax.legend()
    ax.grid(axis='y', alpha=0.35, linestyle='--')
    fig.tight_layout()

    img = os.path.join(LOG_DIR, 'case4_delay_jitter.png')
    fig.savefig(img, dpi=150, bbox_inches='tight'); plt.close(fig)

    with open(img.replace('.png', '.csv'), 'w', newline='') as f:
        csv.writer(f).writerows(
            [['Src','Dst','Scenario','RTT_Avg_ms','Jitter_ms','Loss_Pct']] + raw_rows)

    log(f'  → Lưu: {img}')
    return img


# ═══════════════════════════════════════════════════════════════════════════
# CASE 5: STRESS TEST
# ═══════════════════════════════════════════════════════════════════════════

def case5_stress():
    log('\n' + '═'*55)
    log('CASE 5: Stress Test - Hiệu năng khi backbone chịu tải')
    log('═'*55)

    scenarios = [
        dict(m_src='admin1', m_dst='db1',
             s_src='h1',     s_dst='web1',
             label='admin1→db1\n(stress: h1→web1)'),
        dict(m_src='h1',     m_dst='web1',
             s_src='admin1', s_dst='db1',
             label='h1→web1\n(stress: admin1→db1)'),
    ]

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    all_rows = []

    for idx, sc in enumerate(scenarios):
        log(f'\n  Kịch bản {idx+1}: Đo {sc["m_src"]}→{sc["m_dst"]}')

        # Baseline (không tải)
        log(f'    Baseline (không tải)...')
        base = measure_ping(sc['m_src'], sc['m_dst'], count=10)

        # Bật stress load
        dst_ip = get_ip(sc['s_dst'])
        ns(sc['s_dst'], 'killall -9 iperf iperf3 2>/dev/null')
        ns(sc['s_dst'], 'iperf -s -D 2>/dev/null')
        time.sleep(0.3)
        ns(sc['s_src'], f'iperf -c {dst_ip} -t 15 -P 4 > /dev/null 2>&1 &')
        time.sleep(2)
        log(f'    Under Load (stress iperf đang chạy)...')
        load = measure_ping(sc['m_src'], sc['m_dst'], count=10)

        # Dừng stress
        ns(sc['s_src'], 'killall -9 iperf iperf3 2>/dev/null')
        ns(sc['s_dst'], 'killall -9 iperf iperf3 2>/dev/null')
        time.sleep(1)

        bv = [max(base['rtt_avg'], 0), max(base['jitter'], 0), base['loss_pct']]
        lv = [max(load['rtt_avg'], 0), max(load['jitter'], 0), load['loss_pct']]
        cats = ['RTT Avg (ms)', 'Jitter (ms)', 'Loss (%)']

        log(f'    Baseline : RTT={bv[0]:.2f}ms Jitter={bv[1]:.2f}ms Loss={bv[2]:.0f}%')
        log(f'    Under Load: RTT={lv[0]:.2f}ms Jitter={lv[1]:.2f}ms Loss={lv[2]:.0f}%')

        all_rows.append((sc['m_src'], sc['m_dst'], sc['s_src'], sc['s_dst'],
                         *bv, *lv))

        ax = axes[idx]
        xp = np.arange(3)
        ww = 0.36
        ax.bar(xp - ww/2, bv, ww, color='#2a9d8f', label='Baseline',      edgecolor='white')
        ax.bar(xp + ww/2, lv, ww, color='#e76f51', label='Under Stress',   edgecolor='white')
        ax.set_xticks(xp); ax.set_xticklabels(cats)
        ax.set_title(sc['label'], fontsize=10, fontweight='bold')
        ax.legend(fontsize=8)
        ax.grid(axis='y', alpha=0.35, linestyle='--')

    fig.suptitle('CASE 5: Stress Test — Tác động của tải đến hiệu năng MPLS\n'
                 '(Baseline vs. Under Stress Load)',
                 fontweight='bold', fontsize=12)
    fig.tight_layout()

    img = os.path.join(LOG_DIR, 'case5_stress.png')
    fig.savefig(img, dpi=150, bbox_inches='tight'); plt.close(fig)

    hdrs = ['Msr_Src','Msr_Dst','Stress_Src','Stress_Dst',
            'Base_RTT','Base_Jit','Base_Loss',
            'Load_RTT','Load_Jit','Load_Loss']
    with open(img.replace('.png', '.csv'), 'w', newline='') as f:
        csv.writer(f).writerows([hdrs] + all_rows)

    log(f'  → Lưu: {img}')
    return img


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

CASE_MAP = {
    1: ('Ping Matrix',           case1_ping_matrix),
    2: ('Path / Tracepath',      case2_path_trace),
    3: ('Throughput (iperf)',     case3_throughput),
    4: ('Delay / Jitter / Loss', case4_delay_jitter),
    5: ('Stress Test',           case5_stress),
}


def main():
    print()
    print('╔══════════════════════════════════════════════════════════╗')
    print('║  METRO ETHERNET MPLS - NETWORK MEASUREMENT TOOL (CLI)   ║')
    print('╠══════════════════════════════════════════════════════════╣')
    print(f'║  Kết quả lưu tại: {LOG_DIR:<39}║')
    print('╚══════════════════════════════════════════════════════════╝')
    print()

    if not check_netns():
        sys.exit(1)

    # Parse danh sách case từ args
    cases_to_run = list(CASE_MAP.keys())  # mặc định: tất cả

    for arg in sys.argv[1:]:
        if arg.startswith('--case'):
            val = arg.split('=')[-1] if '=' in arg else (sys.argv[sys.argv.index(arg)+1]
                                                          if sys.argv.index(arg)+1 < len(sys.argv) else '')
            # Lấy số sau --case hoặc --cases
            if not val and len(sys.argv) > sys.argv.index(arg) + 1:
                val = sys.argv[sys.argv.index(arg) + 1]
            try:
                cases_to_run = [int(c.strip()) for c in val.replace(',', ' ').split()]
            except ValueError:
                pass

    print(f'Cases sẽ chạy: {cases_to_run}')
    print()

    t_start = time.time()
    saved = []

    for i in cases_to_run:
        if i not in CASE_MAP:
            log(f'  ⚠ Case {i} không tồn tại, bỏ qua.')
            continue
        name, func = CASE_MAP[i]
        try:
            img = func()
            saved.append(img)
        except Exception as e:
            log(f'  [LỖI] Case {i} ({name}): {e}')

    elapsed = time.time() - t_start
    print()
    print('╔══════════════════════════════════════════════════════════╗')
    print(f'║  HOÀN TẤT: {len(saved)}/{len(cases_to_run)} case  —  {elapsed:.0f}s              ')
    print(f'║  Kết quả: {LOG_DIR}')
    print('╠══════════════════════════════════════════════════════════╣')
    for f in saved:
        print(f'║  ✓ {os.path.basename(f)}')
    print('╚══════════════════════════════════════════════════════════╝')


if __name__ == '__main__':
    if os.geteuid() != 0:
        print('Cần chạy với sudo: sudo python3 tool.py')
        sys.exit(1)
    main()

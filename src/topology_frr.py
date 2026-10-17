#!/usr/bin/env python3
"""
=============================================================================
METRO ETHERNET MPLS v5 – FRR OSPF + LDP + VPLS
=============================================================================
Tuân thủ đầy đủ huongdan.txt:
  Dòng 5  : modprobe mpls_router mpls_iptunnel              ✓
  Dòng 7-9: Class Router CE/PE/P (FRRRouter + LinuxRouter)  ✓
  Dòng 10 : IP interfaces + Loopback PE/P                   ✓
  Dòng 16 : OSPF nội bộ Branch 3 (Leaf-Spine)              ✓
  Dòng 17 : Static Route CE→PE                              ✓
  Dòng 19 : OSPF Underlay backbone PE/P (FRR ospfd)         ✓
  Dòng 20 : LDP phân phối nhãn tự động (FRR ldpd)           ✓
  Dòng 21-23: VPLS Bridge-group + GRE Pseudowire            ✓

Yêu cầu:
  - FRRouting 8.4.4+  (sudo apt install frr frr-pythontools)
  - Mininet + OVS
  - Linux kernel modules: mpls_router, mpls_iptunnel

Chạy:
  sudo python3 topology_frr.py             → CLI Mininet (chờ ~65s hội tụ)
  sudo python3 topology_frr.py --test      → Quick connectivity test
  sudo python3 topology_frr.py --clean     → Dọn dẹp
  sudo python3 topology_frr.py --status    → Xem OSPF/LDP status

So sánh với topology_static.py:
  topology_static.py  → Static Routes + Static MPLS Labels (phiên bản cũ)
  topology_frr.py     → FRR OSPF + LDP + VPLS        (phiên bản mới, file này)
=============================================================================
"""

import os
import sys
import subprocess
import time

from mininet.net import Mininet
from mininet.node import Host, OVSSwitch
from mininet.link import Link
from mininet.cli import CLI
from mininet.log import setLogLevel, info

# ─────────────────────────────────────────────────────────────────────────────
# FRR DAEMON PATHS & VTY PORTS
# ─────────────────────────────────────────────────────────────────────────────
FRR_DIR    = '/usr/lib/frr'
ZEBRA_BIN  = f'{FRR_DIR}/zebra'
OSPFD_BIN  = f'{FRR_DIR}/ospfd'
LDPD_BIN   = f'{FRR_DIR}/ldpd'

ZEBRA_PORT = 2601   # Zebra VTY TCP port
OSPFD_PORT = 2604   # ospfd VTY TCP port
LDPD_PORT  = 2612   # ldpd  VTY TCP port


# ─────────────────────────────────────────────────────────────────────────────
# BASE CLASS: LinuxRouter  (dùng cho CE không cần FRR)
# ─────────────────────────────────────────────────────────────────────────────
class LinuxRouter(Host):
    """Router đơn giản dùng Linux kernel – dùng cho CE1, CE2."""

    def config(self, **params):
        super().config(**params)
        self.cmd('sysctl -w net.ipv4.ip_forward=1')
        self.cmd('for f in /proc/sys/net/ipv4/conf/*/rp_filter; do echo 0 > $f; done')

    def terminate(self):
        self.cmd('sysctl -w net.ipv4.ip_forward=0')
        super().terminate()

    def s(self, intf, cidr, mpls=False):
        """Gán IP cho interface."""
        self.cmd(f'ip link set {intf} up')
        self.cmd(f'ip addr flush dev {intf}')
        self.cmd(f'ip addr add {cidr} dev {intf}')
        if mpls:
            self.cmd(f'sysctl -w net.mpls.conf.{intf}.input=1 2>/dev/null || true')

    def r(self, prefix, via):
        """Thêm static route."""
        self.cmd(f'ip route del {prefix} 2>/dev/null')
        self.cmd(f'ip route add {prefix} via {via} 2>/dev/null || true')

    def r_ecmp(self, prefix, nexthops):
        """Thêm ECMP route."""
        self.cmd(f'ip route del {prefix} 2>/dev/null')
        nh = ' '.join(f'nexthop via {n}' for n in nexthops)
        self.cmd(f'ip route add {prefix} {nh} 2>/dev/null || true')


# ─────────────────────────────────────────────────────────────────────────────
# FRR CLASS: FRRRouter  (dùng cho P, PE, CE3, Branch-3 Leaf/Spine)
# ─────────────────────────────────────────────────────────────────────────────
class FRRRouter(LinuxRouter):
    """
    Router chạy FRRouting v8.4+: zebra + ospfd + ldpd.

    Sử dụng Mininet privateDirs để cô lập /var/run/frr cho từng node,
    đảm bảo zserv.api không bị share giữa các router.
    """
    def __init__(self, name, **params):
        params.setdefault('privateDirs', [])
        params['privateDirs'].append('/var/run/frr')
        super().__init__(name, **params)

    def config(self, **params):
        super().config(**params)
        self.cmd('sysctl -w net.mpls.platform_labels=65536 2>/dev/null || true')
        
        # Đảm bảo thư mục privateDirs thuộc quyền user frr
        self.cmd('chown frr:frr /var/run/frr 2>/dev/null || true')

        conf_dir = f'/tmp/frr_{self.name}'
        self.cmd(f'rm -rf {conf_dir} && mkdir -p {conf_dir} && chmod 777 {conf_dir}')

        # Viết file cấu hình cơ bản cho từng daemon
        base = (
            f'hostname {self.name}\n'
            'log stdout\n'
            'service advanced-vty\n'
            '!\n'
            'line vty\n'
            ' no login\n'
            '!\n'
        )
        for fname in ['zebra.conf', 'ospfd.conf', 'ldpd.conf']:
            path = f'{conf_dir}/{fname}'
            with open(path, 'w') as f:
                f.write(base)
            os.system(f'chown frr:frr {path} 2>/dev/null')
        os.system(f'chown frr:frr {conf_dir} 2>/dev/null')

        # Khởi động Zebra (tự động mở /var/run/frr/zserv.api riêng cho node này)
        self.cmd(
            f'{ZEBRA_BIN} -d -u frr -g frr -A 127.0.0.1 '
            f'-f {conf_dir}/zebra.conf -i {conf_dir}/zebra.pid 2>/dev/null || true'
        )
        time.sleep(0.3)

        # KHÔNG khởi động ospfd hay ldpd ở đây.
        # cfg_ospf() và cfg_ldp() sẽ khởi động daemon SAU KHI IP đã được cấu hình.

    def terminate(self):
        conf_dir = f'/tmp/frr_{self.name}'
        for daemon in ['ldpd', 'ospfd', 'zebra']:
            self.cmd(f'kill $(cat {conf_dir}/{daemon}.pid 2>/dev/null) 2>/dev/null; true')
        self.cmd(f'rm -rf {conf_dir}')
        super().terminate()

    def _apply_conf(self, daemon, extra_body):
        """
        Ghi config vào file conf rồi reload daemon bằng SIGHUP.
        Đây là cách đáng tin cậy nhất: không phụ thuộc vào TCP VTY readiness.
        """
        conf_dir = f'/tmp/frr_{self.name}'
        pid_file = f'{conf_dir}/{daemon}.pid'
        conf_file = f'{conf_dir}/{daemon}.conf'

        base = (
            f'hostname {self.name}\n'
            'log stdout\n'
            'service advanced-vty\n'
            '!\n'
            'line vty\n'
            ' no login\n'
            '!\n'
        )
        content = base + extra_body + '\n'
        with open(conf_file, 'w') as f:
            f.write(content)
        os.system(f'chown frr:frr {conf_file} 2>/dev/null')
        # SIGHUP → daemon reload conf file
        self.cmd(f'kill -HUP $(cat {pid_file} 2>/dev/null) 2>/dev/null; true')
        time.sleep(0.3)

    def _nc_send(self, port, body):
        """Legacy fallback: gửi config qua TCP VTY (pipe strings chống UnicodeError)."""
        tmpfile = f'/tmp/frr_{self.name}_cfg_{port}.txt'
        full = f'enable\nconf t\n{body}\nend\nwr\nexit\n'
        with open(tmpfile, 'w') as f:
            f.write(full)
        for attempt in range(5):
            out = self.cmd(
                f'nc -w 5 127.0.0.1 {port} < {tmpfile} 2>/dev/null'
                f' | strings 2>/dev/null; true'
            )
            if out and ('#' in out or 'hostname' in out.lower()):
                break
            time.sleep(1)

    def cfg_ospf(self, body):
        """
        Khởi động ospfd sau khi ghi config đầy đủ vào file riêng.
        FRR 8.4 ospfd KHÔNG reload networks khi nhận SIGHUP, nên phải
        khởi động nó với file cấu hình hoàn chỉnh ngay từ đầu.
        """
        conf_dir  = f'/tmp/frr_{self.name}'
        pid_file  = f'{conf_dir}/ospfd.pid'
        conf_file = f'{conf_dir}/ospfd_full.conf'

        base = (
            f'hostname {self.name}\n'
            'log stdout\n'
            'service advanced-vty\n'
            '!\n'
            'line vty\n'
            ' no login\n'
            '!\n'
        )
        with open(conf_file, 'w') as f:
            f.write(base + body + '\n')
        os.system(f'chown frr:frr {conf_file} 2>/dev/null')

        # Kill ospfd cũ nếu có
        self.cmd(f'kill $(cat {pid_file} 2>/dev/null) 2>/dev/null; true')
        time.sleep(0.2)

        # Khởi động ospfd với config đầy đủ
        self.cmd(
            f'{OSPFD_BIN} -d -u frr -g frr -A 127.0.0.1 '
            f'-f {conf_file} -i {pid_file} 2>/dev/null || true'
        )

    def cfg_ldp(self, body):
        """
        Khởi động ldpd sau khi ghi config đầy đủ vào file riêng.
        Nhờ privateDirs=['/var/run/frr'], zserv.api đã được cô lập an toàn,
        không cần nsenter hay -z flags nữa.
        """
        conf_dir  = f'/tmp/frr_{self.name}'
        pid_file  = f'{conf_dir}/ldpd.pid'
        conf_file = f'{conf_dir}/ldp_full.conf'

        base = (
            f'hostname {self.name}\n'
            'log stdout\n'
            'service advanced-vty\n'
            '!\n'
            'line vty\n'
            ' no login\n'
            '!\n'
        )
        with open(conf_file, 'w') as f:
            f.write(base + body + '\n')
        os.system(f'chown frr:frr {conf_file} 2>/dev/null')

        # Kill ldpd cũ nếu có
        self.cmd(f'kill $(cat {pid_file} 2>/dev/null) 2>/dev/null; true')
        time.sleep(0.2)

        # Khởi động ldpd (tự động dùng zebra socket từ /var/run/frr cô lập của node)
        self.cmd(
            f'{LDPD_BIN} -d -u frr -g frr -A 127.0.0.1 '
            f'-f {conf_file} -i {pid_file} 2>/dev/null || true'
        )

    def show_ospf(self):
        """Lấy thông tin OSPF neighbor."""
        tmpfile = f'/tmp/frr_{self.name}_show_ospf.txt'
        with open(tmpfile, 'w') as f:
            f.write('show ip ospf neighbor\nquit\n')
        return self.cmd(f'nc -w 3 127.0.0.1 {OSPFD_PORT} < {tmpfile} 2>/dev/null')

    def show_ldp(self):
        """Lấy LDP bindings."""
        tmpfile = f'/tmp/frr_{self.name}_show_ldp.txt'
        with open(tmpfile, 'w') as f:
            f.write('show mpls ldp binding\nquit\n')
        return self.cmd(f'nc -w 3 127.0.0.1 {LDPD_PORT} < {tmpfile} 2>/dev/null')


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def cleanup():
    info('*** Cleanup...\n')
    subprocess.run(['mn', '-c'], capture_output=True, timeout=30)
    os.system('killall -9 zebra ospfd ldpd 2>/dev/null; true')
    # Xóa stale kernel MPLS routes từ lần chạy trước
    os.system('ip -f mpls route flush 2>/dev/null; true')
    # Xóa custom-named interfaces (mn -c chỉ xóa OVS bridges, không xóa veth pairs)
    _stale_intfs = [
        # Branch 2 Core
        'b2c1-ce2','b2c1-d1','b2c1-d2',
        'b2c2-ce2','b2c2-d1','b2c2-d2',
        # Branch 2 Distribution
        'b2d1-c1','b2d1-c2','b2d1-a1','b2d1-a2','b2d1-a3',
        'b2d2-c1','b2d2-c2','b2d2-a1','b2d2-a2','b2d2-a3',
        # Branch 2 Access
        'b2a1-d1','b2a1-d2','b2a1-adm1','b2a1-adm2',
        'b2a2-d1','b2a2-d2','b2a2-lab1','b2a2-lab2',
        'b2a3-d1','b2a3-d2','b2a3-gst1','b2a3-gst2',
        # Branch 3 Bridges (trên lf nodes)
        'br-web','br-dns','br-db',
        # VPLS Bridges + GRE Pseudowires (trên PE nodes)
        'br-vpls',
        'pw_pe1_pe2','pw_pe1_pe3',
        'pw_pe2_pe1','pw_pe2_pe3',
        'pw_pe3_pe1','pw_pe3_pe2',
        'gretap0',
    ]
    for intf in _stale_intfs:
        os.system(f'ip link del {intf} 2>/dev/null; true')



def setup_kernel():
    info('*** Setup MPLS kernel modules...\n')
    for m in ['mpls_router', 'mpls_iptunnel']:
        ret = os.system(f'modprobe {m} 2>/dev/null')
        info(f'    {m}: {"OK" if ret == 0 else "warn"}\n')
    os.system('sysctl -w net.mpls.platform_labels=65536 2>/dev/null')
    os.system('sysctl -w net.mpls.conf.lo.input=1 2>/dev/null')


def create_netns_symlinks(net):
    os.makedirs('/var/run/netns', exist_ok=True)
    for name, node in net.items():
        try:
            link = f'/var/run/netns/{name}'
            if os.path.islink(link):
                os.remove(link)
            os.symlink(f'/proc/{node.pid}/ns/net', link)
        except Exception:
            pass
    info('*** Named netns symlinks created (tool.py ready)\n')


def remove_netns_symlinks(net):
    for name in net:
        link = f'/var/run/netns/{name}'
        if os.path.islink(link):
            try:
                os.remove(link)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# BUILD TOPOLOGY  (cùng cấu trúc vật lý với topology_static.py)
# ─────────────────────────────────────────────────────────────────────────────
def build_net():
    cleanup()
    setup_kernel()
    net = Mininet(controller=None, link=Link, autoSetMacs=True)

    info('*** Tạo nodes...\n')

    # ── Backbone: P Routers (FRR: OSPF + LDP) ────────────────────────────────
    p1 = net.addHost('p1', cls=FRRRouter, ip='127.0.0.1/8')
    p2 = net.addHost('p2', cls=FRRRouter, ip='127.0.0.1/8')
    p3 = net.addHost('p3', cls=FRRRouter, ip='127.0.0.1/8')
    p4 = net.addHost('p4', cls=FRRRouter, ip='127.0.0.1/8')

    # ── Backbone: PE Routers (FRR: OSPF + LDP) ───────────────────────────────
    pe1 = net.addHost('pe1', cls=FRRRouter, ip='127.0.0.1/8')
    pe2 = net.addHost('pe2', cls=FRRRouter, ip='127.0.0.1/8')
    pe3 = net.addHost('pe3', cls=FRRRouter, ip='127.0.0.1/8')

    # ── CE Routers ────────────────────────────────────────────────────────────
    ce1 = net.addHost('ce1', cls=LinuxRouter, ip='127.0.0.1/8')   # static only
    ce2 = net.addHost('ce2', cls=LinuxRouter, ip='127.0.0.1/8')   # static only
    ce3 = net.addHost('ce3', cls=FRRRouter,   ip='127.0.0.1/8')   # FRR: B3 OSPF

    # ── Branch 1: Mạng Phẳng (Flat) ──────────────────────────────────────────
    sw1 = net.addSwitch('sw1', cls=OVSSwitch, failMode='standalone')
    sw2 = net.addSwitch('sw2', cls=OVSSwitch, failMode='standalone')
    h1  = net.addHost('h1',  ip='10.1.0.1/24', defaultRoute='via 10.1.0.254')
    h2  = net.addHost('h2',  ip='10.1.0.2/24', defaultRoute='via 10.1.0.254')
    h3  = net.addHost('h3',  ip='10.1.0.3/24', defaultRoute='via 10.1.0.254')
    h4  = net.addHost('h4',  ip='10.1.0.4/24', defaultRoute='via 10.1.0.254')

    # ── Branch 2: 3-Layer (Core-Distribution-Access) ─────────────────────────
    SW = dict(cls=OVSSwitch, failMode='standalone', stp=True)
    b2c1 = net.addSwitch('b2c1', **SW); b2c2 = net.addSwitch('b2c2', **SW)
    b2d1 = net.addSwitch('b2d1', **SW); b2d2 = net.addSwitch('b2d2', **SW)
    b2a1 = net.addSwitch('b2a1', **SW); b2a2 = net.addSwitch('b2a2', **SW)
    b2a3 = net.addSwitch('b2a3', **SW)
    admin1 = net.addHost('admin1', ip='10.2.10.1/24', defaultRoute='via 10.2.10.254')
    admin2 = net.addHost('admin2', ip='10.2.10.2/24', defaultRoute='via 10.2.10.254')
    lab1   = net.addHost('lab1',   ip='10.2.20.1/24', defaultRoute='via 10.2.20.254')
    lab2   = net.addHost('lab2',   ip='10.2.20.2/24', defaultRoute='via 10.2.20.254')
    guest1 = net.addHost('guest1', ip='10.2.30.1/24', defaultRoute='via 10.2.30.254')
    guest2 = net.addHost('guest2', ip='10.2.30.2/24', defaultRoute='via 10.2.30.254')

    # ── Branch 3: Leaf-Spine (FRR: OSPF nội bộ) ──────────────────────────────
    b3sp1 = net.addHost('b3sp1', cls=FRRRouter, ip='127.0.0.1/8')
    b3sp2 = net.addHost('b3sp2', cls=FRRRouter, ip='127.0.0.1/8')
    b3lf1 = net.addHost('b3lf1', cls=FRRRouter, ip='127.0.0.1/8')
    b3lf2 = net.addHost('b3lf2', cls=FRRRouter, ip='127.0.0.1/8')
    b3lf3 = net.addHost('b3lf3', cls=FRRRouter, ip='127.0.0.1/8')
    b3lf4 = net.addHost('b3lf4', cls=FRRRouter, ip='127.0.0.1/8')
    web1 = net.addHost('web1', ip='10.3.10.1/24', defaultRoute='via 10.3.10.254')
    web2 = net.addHost('web2', ip='10.3.10.2/24', defaultRoute='via 10.3.10.254')
    dns1 = net.addHost('dns1', ip='10.3.20.1/24', defaultRoute='via 10.3.20.254')
    dns2 = net.addHost('dns2', ip='10.3.20.2/24', defaultRoute='via 10.3.20.254')
    db1  = net.addHost('db1',  ip='10.3.30.1/24', defaultRoute='via 10.3.30.254')
    db2  = net.addHost('db2',  ip='10.3.30.2/24', defaultRoute='via 10.3.30.254')

    info('*** Tạo links...\n')

    # ── Backbone P-P (Full Mesh) ──────────────────────────────────────────────
    net.addLink(p1, p2, intfName1='p1-p2', intfName2='p2-p1')
    net.addLink(p1, p3, intfName1='p1-p3', intfName2='p3-p1')
    net.addLink(p1, p4, intfName1='p1-p4', intfName2='p4-p1')
    net.addLink(p2, p3, intfName1='p2-p3', intfName2='p3-p2')
    net.addLink(p2, p4, intfName1='p2-p4', intfName2='p4-p2')
    net.addLink(p3, p4, intfName1='p3-p4', intfName2='p4-p3')

    # ── PE-P ─────────────────────────────────────────────────────────────────
    net.addLink(pe1, p1, intfName1='pe1-p1', intfName2='p1-pe1')
    net.addLink(pe1, p3, intfName1='pe1-p3', intfName2='p3-pe1')
    net.addLink(pe2, p3, intfName1='pe2-p3', intfName2='p3-pe2')
    net.addLink(pe2, p4, intfName1='pe2-p4', intfName2='p4-pe2')
    net.addLink(pe3, p2, intfName1='pe3-p2', intfName2='p2-pe3')
    net.addLink(pe3, p4, intfName1='pe3-p4', intfName2='p4-pe3')

    # ── CE-PE ────────────────────────────────────────────────────────────────
    net.addLink(ce1, pe1, intfName1='ce1-pe1', intfName2='pe1-ce1')
    net.addLink(ce2, pe2, intfName1='ce2-pe2', intfName2='pe2-ce2')
    net.addLink(ce3, pe3, intfName1='ce3-pe3', intfName2='pe3-ce3')

    # ── Branch 1 (Flat) ───────────────────────────────────────────────────────
    net.addLink(ce1, sw1, intfName1='ce1-sw1')
    net.addLink(sw1, sw2)
    net.addLink(h1, sw1, intfName1='h1-eth0')
    net.addLink(h2, sw1, intfName1='h2-eth0')
    net.addLink(h3, sw2, intfName1='h3-eth0')
    net.addLink(h4, sw2, intfName1='h4-eth0')

    # ── Branch 2 (3-Layer) – explicit intfNames để VLAN config chính xác ────────
    net.addLink(ce2,  b2c1, intfName1='ce2-c1',   intfName2='b2c1-ce2')
    net.addLink(ce2,  b2c2, intfName1='ce2-c2',   intfName2='b2c2-ce2')
    net.addLink(b2c1, b2d1, intfName1='b2c1-d1',  intfName2='b2d1-c1')
    net.addLink(b2c1, b2d2, intfName1='b2c1-d2',  intfName2='b2d2-c1')
    net.addLink(b2c2, b2d1, intfName1='b2c2-d1',  intfName2='b2d1-c2')
    net.addLink(b2c2, b2d2, intfName1='b2c2-d2',  intfName2='b2d2-c2')
    net.addLink(b2d1, b2a1, intfName1='b2d1-a1',  intfName2='b2a1-d1')
    net.addLink(b2d2, b2a1, intfName1='b2d2-a1',  intfName2='b2a1-d2')
    net.addLink(b2d1, b2a2, intfName1='b2d1-a2',  intfName2='b2a2-d1')
    net.addLink(b2d2, b2a2, intfName1='b2d2-a2',  intfName2='b2a2-d2')
    net.addLink(b2d1, b2a3, intfName1='b2d1-a3',  intfName2='b2a3-d1')
    net.addLink(b2d2, b2a3, intfName1='b2d2-a3',  intfName2='b2a3-d2')
    net.addLink(admin1, b2a1, intfName1='admin1-eth0', intfName2='b2a1-adm1')
    net.addLink(admin2, b2a1, intfName1='admin2-eth0', intfName2='b2a1-adm2')
    net.addLink(lab1,   b2a2, intfName1='lab1-eth0',   intfName2='b2a2-lab1')
    net.addLink(lab2,   b2a2, intfName1='lab2-eth0',   intfName2='b2a2-lab2')
    net.addLink(guest1, b2a3, intfName1='guest1-eth0', intfName2='b2a3-gst1')
    net.addLink(guest2, b2a3, intfName1='guest2-eth0', intfName2='b2a3-gst2')

    # ── Branch 3 (Leaf-Spine) ─────────────────────────────────────────────────
    net.addLink(ce3,   b3lf4, intfName1='ce3-lf4',  intfName2='lf4-ce3')
    net.addLink(b3lf4, b3sp1, intfName1='lf4-sp1',  intfName2='sp1-lf4')
    net.addLink(b3lf4, b3sp2, intfName1='lf4-sp2',  intfName2='sp2-lf4')
    net.addLink(b3lf1, b3sp1, intfName1='lf1-sp1',  intfName2='sp1-lf1')
    net.addLink(b3lf1, b3sp2, intfName1='lf1-sp2',  intfName2='sp2-lf1')
    net.addLink(b3lf2, b3sp1, intfName1='lf2-sp1',  intfName2='sp1-lf2')
    net.addLink(b3lf2, b3sp2, intfName1='lf2-sp2',  intfName2='sp2-lf2')
    net.addLink(b3lf3, b3sp1, intfName1='lf3-sp1',  intfName2='sp1-lf3')
    net.addLink(b3lf3, b3sp2, intfName1='lf3-sp2',  intfName2='sp2-lf3')
    net.addLink(web1, b3lf1, intfName1='web1-eth0', intfName2='lf1-web1')
    net.addLink(web2, b3lf1, intfName1='web2-eth0', intfName2='lf1-web2')
    net.addLink(dns1, b3lf2, intfName1='dns1-eth0', intfName2='lf2-dns1')
    net.addLink(dns2, b3lf2, intfName1='dns2-eth0', intfName2='lf2-dns2')
    net.addLink(db1,  b3lf3, intfName1='db1-eth0',  intfName2='lf3-db1')
    net.addLink(db2,  b3lf3, intfName1='db2-eth0',  intfName2='lf3-db2')

    net.start()
    create_netns_symlinks(net)   # Tạo trước configure_all để cfg_ldp dùng được
    configure_all(net)
    return net


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE ALL  (theo đúng thứ tự hướng dẫn)
# ─────────────────────────────────────────────────────────────────────────────
def configure_all(net):
    """
    Thứ tự cấu hình (đã sửa để LDP hoạt động đúng):
      [1] IP interfaces + Loopback
      [2] Static Routes: CE-PE + Customer Prefixes  ← TRƯỚC OSPF/LDP
                (LDP cần thấy các prefix này để phân phối label đúng)
      [3] OSPF Underlay Backbone (ospfd trên PE/P)
      [4] LDP phân phối nhãn (ldpd trên PE/P)
      [5] OSPF nội bộ Branch 3 (ospfd trên CE3/SP/LF)
      [Chờ 45s] OSPF + LDP hội tụ đầy đủ với toàn bộ prefix
      [6] Branch 2 STP hội tụ (song song với wait trên nếu đủ thời gian)
      [7] VPLS Bridge-group + GRE Pseudowire
    """
    info('═' * 62 + '\n')
    info('*** [1/7] Cấu hình IP Addresses...\n')
    configure_ip_addresses(net)

    # ĐẶT STATIC ROUTES TRƯỚC OSPF/LDP để LDP học đủ prefix từ đầu.
    # Nếu static routes được thêm SAU khi LDP đã hội tụ, LDP sẽ không
    # phân phối label cho B1/B2/B3 customer prefixes kịp thời → path dài
    # (PE1→P1→P2→PE3) bị fail vì label chain chưa đầy đủ.
    info('*** [2/7] Cấu hình Static Routes (CE-PE + Customer Prefixes)...\n')
    setup_static_routes(net)

    info('*** [3/7] Cấu hình OSPF Underlay Backbone (PE/P)...\n')
    setup_ospf_backbone(net)

    info('*** [4/7] Cấu hình LDP phân phối nhãn (PE/P)...\n')
    setup_ldp_backbone(net)

    info('*** [5/7] Cấu hình OSPF nội bộ Branch 3 Leaf-Spine...\n')
    setup_branch3_ospf(net)

    # Tăng wait từ 35s → 45s: đảm bảo OSPF hội tụ đầy đủ (7 routers full mesh)
    # và LDP phân phối label cho tất cả customer prefix (B1/B2/B3).
    info('*** Chờ OSPF + LDP hội tụ (45s)...\n')
    time.sleep(45)

    info('*** [6/8] Cấu hình VLAN 802.1Q Branch 2 (Core-Dist-Access)...\n')
    setup_branch2_vlan(net)

    info('*** [7/8] Chờ STP hội tụ Branch 2 (15s)...\n')
    time.sleep(15)

    info('*** [8/8] Cấu hình VPLS Bridge-group + GRE Pseudowire...\n')
    setup_vpls(net)

    info('═' * 62 + '\n')
    info('*** MẠNG SẴN SÀNG! (FRR OSPF + LDP + VPLS)\n')
    print_summary()



# ─────────────────────────────────────────────────────────────────────────────
# [1] IP ADDRESSES
# ─────────────────────────────────────────────────────────────────────────────
def configure_ip_addresses(net):
    p1  = net['p1'];  p2  = net['p2'];  p3  = net['p3'];  p4  = net['p4']
    pe1 = net['pe1']; pe2 = net['pe2']; pe3 = net['pe3']
    ce1 = net['ce1']; ce2 = net['ce2']; ce3 = net['ce3']
    b3sp1 = net['b3sp1']; b3sp2 = net['b3sp2']
    b3lf1 = net['b3lf1']; b3lf2 = net['b3lf2']
    b3lf3 = net['b3lf3']; b3lf4 = net['b3lf4']

    # ── Loopback IPs (dùng làm Router-ID cho OSPF và transport-address cho LDP)
    for node, lo_ip in [
        (p1,  '10.0.0.11'), (p2,  '10.0.0.12'),
        (p3,  '10.0.0.13'), (p4,  '10.0.0.14'),
        (pe1, '10.0.0.1'),  (pe2, '10.0.0.2'),  (pe3, '10.0.0.3'),
    ]:
        node.cmd(f'ip addr add {lo_ip}/32 dev lo 2>/dev/null || true')

    # ── P-P Links (/30 point-to-point) ───────────────────────────────────────
    p1.s('p1-p2','10.100.12.1/30',mpls=True);  p2.s('p2-p1','10.100.12.2/30',mpls=True)
    p1.s('p1-p3','10.100.13.1/30',mpls=True);  p3.s('p3-p1','10.100.13.2/30',mpls=True)
    p1.s('p1-p4','10.100.14.1/30',mpls=True);  p4.s('p4-p1','10.100.14.2/30',mpls=True)
    p2.s('p2-p3','10.100.23.1/30',mpls=True);  p3.s('p3-p2','10.100.23.2/30',mpls=True)
    p2.s('p2-p4','10.100.24.1/30',mpls=True);  p4.s('p4-p2','10.100.24.2/30',mpls=True)
    p3.s('p3-p4','10.100.34.1/30',mpls=True);  p4.s('p4-p3','10.100.34.2/30',mpls=True)

    # ── PE-P Links (/30, bật MPLS input) ─────────────────────────────────────
    pe1.s('pe1-p1','10.100.101.1/30',mpls=True); p1.s('p1-pe1','10.100.101.2/30',mpls=True)
    pe1.s('pe1-p3','10.100.103.1/30',mpls=True); p3.s('p3-pe1','10.100.103.2/30',mpls=True)
    pe2.s('pe2-p3','10.100.203.1/30',mpls=True); p3.s('p3-pe2','10.100.203.2/30',mpls=True)
    pe2.s('pe2-p4','10.100.204.1/30',mpls=True); p4.s('p4-pe2','10.100.204.2/30',mpls=True)
    pe3.s('pe3-p2','10.50.32.1/30',  mpls=True); p2.s('p2-pe3','10.50.32.2/30',  mpls=True)
    pe3.s('pe3-p4','10.50.34.1/30',  mpls=True); p4.s('p4-pe3','10.50.34.2/30',  mpls=True)

    # ── CE-PE Links (/30) ─────────────────────────────────────────────────────
    pe1.s('pe1-ce1','172.16.1.2/30'); ce1.s('ce1-pe1','172.16.1.1/30')
    pe2.s('pe2-ce2','172.16.2.2/30'); ce2.s('ce2-pe2','172.16.2.1/30')
    pe3.s('pe3-ce3','172.16.3.2/30'); ce3.s('ce3-pe3','172.16.3.1/30')

    # ── CE1 – Branch 1 Gateway ────────────────────────────────────────────────
    ce1.s('ce1-sw1','10.1.0.254/24')

    # ── CE2 – Branch 2 Inter-VLAN Routing qua 802.1Q subinterfaces ──────────
    # ce2-c1 là trunk port (raw, không có IP) → tạo subinterface per VLAN
    ce2.cmd('ip link set ce2-c1 up; ip addr flush dev ce2-c1')
    ce2.cmd('ip link set ce2-c2 up; ip addr flush dev ce2-c2')  # uplink dự phòng
    for vlan, gw in [(10, '10.2.10.254'), (20, '10.2.20.254'), (30, '10.2.30.254')]:
        ce2.cmd(f'ip link add link ce2-c1 name ce2-c1.{vlan} type vlan id {vlan} 2>/dev/null || true')
        ce2.cmd(f'ip link set ce2-c1.{vlan} up')
        ce2.cmd(f'ip addr add {gw}/24 dev ce2-c1.{vlan} 2>/dev/null || true')

    # ── CE3 – Branch 3 Uplink ─────────────────────────────────────────────────
    ce3.s('ce3-lf4','10.3.0.1/30')

    # ── Branch 3 Leaf-Spine IP Addresses ─────────────────────────────────────
    b3lf4.s('lf4-ce3','10.3.0.2/30')
    b3lf4.s('lf4-sp1','10.3.4.1/30');  b3sp1.s('sp1-lf4','10.3.4.2/30')
    b3lf4.s('lf4-sp2','10.3.5.1/30');  b3sp2.s('sp2-lf4','10.3.5.2/30')
    b3sp1.s('sp1-lf1','10.3.11.2/30'); b3lf1.s('lf1-sp1','10.3.11.1/30')
    b3sp1.s('sp1-lf2','10.3.12.2/30'); b3lf2.s('lf2-sp1','10.3.12.1/30')
    b3sp1.s('sp1-lf3','10.3.13.2/30'); b3lf3.s('lf3-sp1','10.3.13.1/30')
    b3sp2.s('sp2-lf1','10.3.21.2/30'); b3lf1.s('lf1-sp2','10.3.21.1/30')
    b3sp2.s('sp2-lf2','10.3.22.2/30'); b3lf2.s('lf2-sp2','10.3.22.1/30')
    b3sp2.s('sp2-lf3','10.3.23.2/30'); b3lf3.s('lf3-sp2','10.3.23.1/30')
    # ── b3lf1: bridge br-web để nối web1 + web2 vào cùng 10.3.10.0/24 ─────────
    b3lf1.cmd('ip link set lf1-web1 up; ip addr flush dev lf1-web1')
    b3lf1.cmd('ip link set lf1-web2 up; ip addr flush dev lf1-web2')
    b3lf1.cmd('ip link add br-web type bridge 2>/dev/null || true')
    b3lf1.cmd('ip link set lf1-web1 master br-web')
    b3lf1.cmd('ip link set lf1-web2 master br-web')
    b3lf1.cmd('ip link set br-web up')
    b3lf1.cmd('ip addr add 10.3.10.254/24 dev br-web 2>/dev/null || true')
    b3lf1.cmd('sysctl -w net.mpls.conf.br-web.input=0 2>/dev/null || true')

    # ── b3lf2: bridge br-dns để nối dns1 + dns2 vào cùng 10.3.20.0/24 ─────────
    b3lf2.cmd('ip link set lf2-dns1 up; ip addr flush dev lf2-dns1')
    b3lf2.cmd('ip link set lf2-dns2 up; ip addr flush dev lf2-dns2')
    b3lf2.cmd('ip link add br-dns type bridge 2>/dev/null || true')
    b3lf2.cmd('ip link set lf2-dns1 master br-dns')
    b3lf2.cmd('ip link set lf2-dns2 master br-dns')
    b3lf2.cmd('ip link set br-dns up')
    b3lf2.cmd('ip addr add 10.3.20.254/24 dev br-dns 2>/dev/null || true')
    b3lf2.cmd('sysctl -w net.mpls.conf.br-dns.input=0 2>/dev/null || true')

    # ── b3lf3: bridge br-db để nối db1 + db2 vào cùng 10.3.30.0/24 ──────────
    b3lf3.cmd('ip link set lf3-db1 up; ip addr flush dev lf3-db1')
    b3lf3.cmd('ip link set lf3-db2 up; ip addr flush dev lf3-db2')
    b3lf3.cmd('ip link add br-db type bridge 2>/dev/null || true')
    b3lf3.cmd('ip link set lf3-db1 master br-db')
    b3lf3.cmd('ip link set lf3-db2 master br-db')
    b3lf3.cmd('ip link set br-db up')
    b3lf3.cmd('ip addr add 10.3.30.254/24 dev br-db 2>/dev/null || true')
    b3lf3.cmd('sysctl -w net.mpls.conf.br-db.input=0 2>/dev/null || true')

    # Bật MPLS input trên loopback
    for node in [pe1, pe2, pe3, p1, p2, p3, p4]:
        node.cmd('sysctl -w net.mpls.conf.lo.input=1 2>/dev/null || true')


# ─────────────────────────────────────────────────────────────────────────────
# [2] OSPF UNDERLAY BACKBONE  (huongdan.txt dòng 19)
# ─────────────────────────────────────────────────────────────────────────────
def setup_ospf_backbone(net):
    """
    Cấu hình OSPF trên tất cả PE và P router (backbone).
    OSPF area 0 quảng bá loopback + các P2P link backbone.
    CE-facing interface dùng passive-interface (không gửi Hello).
    """
    ospf_cfg = {
        'pe1': {
            'rid': '10.0.0.1',
            'networks': [
                '10.0.0.1/32 area 0',
                '10.100.101.0/30 area 0',
                '10.100.103.0/30 area 0',
            ],
            'passive': ['pe1-ce1'],
        },
        'pe2': {
            'rid': '10.0.0.2',
            'networks': [
                '10.0.0.2/32 area 0',
                '10.100.203.0/30 area 0',
                '10.100.204.0/30 area 0',
            ],
            'passive': ['pe2-ce2'],
        },
        'pe3': {
            'rid': '10.0.0.3',
            'networks': [
                '10.0.0.3/32 area 0',
                '10.50.32.0/30 area 0',
                '10.50.34.0/30 area 0',
            ],
            'passive': ['pe3-ce3'],
        },
        'p1': {
            'rid': '10.0.0.11',
            'networks': [
                '10.0.0.11/32 area 0',
                '10.100.12.0/30 area 0',
                '10.100.13.0/30 area 0',
                '10.100.14.0/30 area 0',
                '10.100.101.0/30 area 0',
            ],
            'passive': [],
        },
        'p2': {
            'rid': '10.0.0.12',
            'networks': [
                '10.0.0.12/32 area 0',
                '10.100.12.0/30 area 0',
                '10.100.23.0/30 area 0',
                '10.100.24.0/30 area 0',
                '10.50.32.0/30 area 0',
            ],
            'passive': [],
        },
        'p3': {
            'rid': '10.0.0.13',
            'networks': [
                '10.0.0.13/32 area 0',
                '10.100.13.0/30 area 0',
                '10.100.23.0/30 area 0',
                '10.100.34.0/30 area 0',
                '10.100.103.0/30 area 0',
                '10.100.203.0/30 area 0',
            ],
            'passive': [],
        },
        'p4': {
            'rid': '10.0.0.14',
            'networks': [
                '10.0.0.14/32 area 0',
                '10.100.14.0/30 area 0',
                '10.100.24.0/30 area 0',
                '10.100.34.0/30 area 0',
                '10.100.204.0/30 area 0',
                '10.50.34.0/30 area 0',
            ],
            'passive': [],
        },
    }

    for name, cfg in ospf_cfg.items():
        node = net[name]
        lines = [
            'router ospf',
            f' ospf router-id {cfg["rid"]}',
        ]
        for n in cfg['networks']:
            lines.append(f' network {n}')
        for p in cfg['passive']:
            lines.append(f' passive-interface {p}')
        body = '\n'.join(lines)
        node.cfg_ospf(body)
        info(f'    {name}: OSPF configured (router-id {cfg["rid"]})\n')


# ─────────────────────────────────────────────────────────────────────────────
# [3] LDP – LABEL DISTRIBUTION PROTOCOL  (huongdan.txt dòng 20)
# ─────────────────────────────────────────────────────────────────────────────
def setup_ldp_backbone(net):
    """
    Cấu hình LDP trên các cổng backbone PE-P và P-P.
    transport-address = loopback IP của mỗi router (để LDP dùng OSPF route).
    LDP sẽ tự động phân phối nhãn cho tất cả prefix trong routing table.
    """
    ldp_cfg = {
        'pe1': {'rid': '10.0.0.1',  'ta': '10.0.0.1',  'intfs': ['pe1-p1', 'pe1-p3']},
        'pe2': {'rid': '10.0.0.2',  'ta': '10.0.0.2',  'intfs': ['pe2-p3', 'pe2-p4']},
        'pe3': {'rid': '10.0.0.3',  'ta': '10.0.0.3',  'intfs': ['pe3-p2', 'pe3-p4']},
        'p1':  {'rid': '10.0.0.11', 'ta': '10.0.0.11', 'intfs': ['p1-p2', 'p1-p3', 'p1-p4', 'p1-pe1']},
        'p2':  {'rid': '10.0.0.12', 'ta': '10.0.0.12', 'intfs': ['p2-p1', 'p2-p3', 'p2-p4', 'p2-pe3']},
        'p3':  {'rid': '10.0.0.13', 'ta': '10.0.0.13', 'intfs': ['p3-p1', 'p3-p2', 'p3-p4', 'p3-pe1', 'p3-pe2']},
        'p4':  {'rid': '10.0.0.14', 'ta': '10.0.0.14', 'intfs': ['p4-p1', 'p4-p2', 'p4-p3', 'p4-pe2', 'p4-pe3']},
    }

    for name, cfg in ldp_cfg.items():
        node = net[name]
        # Interface declarations với 'mpls enable' bắt buộc trong FRR 8.4
        intf_lines = ''.join(
            f'  interface {i}\n'
            f'   mpls enable\n'
            f'  !\n'
            for i in cfg['intfs']
        )
        body = (
            'mpls ldp\n'
            f' router-id {cfg["rid"]}\n'
            ' !\n'
            ' address-family ipv4\n'
            f'  discovery transport-address {cfg["ta"]}\n'
            f'{intf_lines}'
            ' exit-address-family\n'
            '!\n'
        )
        node.cfg_ldp(body)
        info(f'    {name}: LDP configured '
             f'(transport-addr {cfg["ta"]}, {len(cfg["intfs"])} intfs)\n')




# ─────────────────────────────────────────────────────────────────────────────
# [4] OSPF NỘI BỘ BRANCH 3  (huongdan.txt dòng 16)
# ─────────────────────────────────────────────────────────────────────────────
def setup_branch3_ospf(net):
    """
    Cấu hình OSPF nội bộ cho Branch 3 Leaf-Spine (area 1).
    CE3 dùng 'default-information originate always' để quảng bá default route
    cho toàn bộ Leaf/Spine → traffic ra ngoài đi qua CE3 → PE3.
    """
    b3_cfg = {
        'ce3':   {'rid': '10.3.0.1',  'passive': ['ce3-pe3'], 'extra': ' default-information originate always\n redistribute connected subnets'},
        'b3lf4': {'rid': '10.3.0.2',  'passive': [], 'extra': ''},
        'b3sp1': {'rid': '10.3.4.2',  'passive': [], 'extra': ''},
        'b3sp2': {'rid': '10.3.5.2',  'passive': [], 'extra': ''},
        'b3lf1': {'rid': '10.3.11.1', 'passive': ['br-web'], 'extra': ''},
        'b3lf2': {'rid': '10.3.12.1', 'passive': ['br-dns'], 'extra': ''},
        'b3lf3': {'rid': '10.3.13.1', 'passive': ['br-db'],  'extra': ''},
    }

    for name, cfg in b3_cfg.items():
        node = net[name]
        lines = [
            'router ospf',
            f' ospf router-id {cfg["rid"]}',
            ' network 10.3.0.0/16 area 1',
        ]
        for p in cfg['passive']:
            lines.append(f' passive-interface {p}')
        if cfg['extra']:
            lines.extend(cfg['extra'].split('\n'))
        body = '\n'.join(lines)
        node.cfg_ospf(body)
        info(f'    {name}: Branch3 OSPF configured (router-id {cfg["rid"]})\n')


# ─────────────────────────────────────────────────────────────────────────────
# [5] STATIC ROUTES  (huongdan.txt dòng 17 – CE-PE static, PE/P customer routes)
# ─────────────────────────────────────────────────────────────────────────────
def setup_static_routes(net):
    """
    Static Route CE→PE (huongdan dòng 17):
      - CE1/CE2/CE3 dùng static default route lên PE tương ứng.
    Customer Prefix Routes trên PE và P:
      - OSPF chỉ xử lý backbone. Customer prefix (B1/B2/B3) vẫn dùng static.
      - LDP sẽ tự gán label cho các prefix này dựa trên routing table.
    """
    pe1 = net['pe1']; pe2 = net['pe2']; pe3 = net['pe3']
    p1  = net['p1'];  p2  = net['p2'];  p3  = net['p3'];  p4  = net['p4']
    ce1 = net['ce1']; ce2 = net['ce2']; ce3 = net['ce3']

    B1 = ['10.1.0.0/24']
    B2 = ['10.2.10.0/24', '10.2.20.0/24', '10.2.30.0/24']
    B3 = ['10.3.10.0/24', '10.3.20.0/24', '10.3.30.0/24']

    # ── PE1: B1 local via CE1, B2 via P3→PE2, B3 via P1→P2→PE3 ─────────────
    pe1.r('10.1.0.0/24', '172.16.1.1')
    for s in B2: pe1.r(s, '10.100.103.2')
    for s in B3: pe1.r(s, '10.100.101.2')

    # ── PE2: B2 local via CE2, B1 via P3→PE1, B3 via P4→PE3 ─────────────────
    for s in B2: pe2.r(s, '172.16.2.1')
    for s in B1: pe2.r(s, '10.100.203.2')
    for s in B3: pe2.r(s, '10.100.204.2')

    # ── PE3: B3 via CE3 (OSPF learned), B1 via P2→PE1, B2 via P4→PE2 ───────
    for s in B3: pe3.r(s, '172.16.3.1')
    for s in B1: pe3.r(s, '10.50.32.2')
    for s in B2: pe3.r(s, '10.50.34.2')

    # ── P1: B1→PE1, B2→P3, B3→P2 ────────────────────────────────────────────
    for s in B1: p1.r(s, '10.100.101.1')
    for s in B2: p1.r(s, '10.100.13.2')
    for s in B3: p1.r(s, '10.100.12.2')

    # ── P2: B3→PE3, B1→P1, B2→P3 ────────────────────────────────────────────
    for s in B3: p2.r(s, '10.50.32.1')
    for s in B1: p2.r(s, '10.100.12.1')
    for s in B2: p2.r(s, '10.100.23.2')

    # ── P3: B1→PE1, B2→PE2, B3→P2 ───────────────────────────────────────────
    for s in B1: p3.r(s, '10.100.103.1')
    for s in B2: p3.r(s, '10.100.203.1')
    for s in B3: p3.r(s, '10.100.23.1')

    # ── P4: B2→PE2, B3→PE3, B1→P3 ───────────────────────────────────────────
    for s in B2: p4.r(s, '10.100.204.1')
    for s in B3: p4.r(s, '10.50.34.1')
    for s in B1: p4.r(s, '10.100.34.1')

    # ── CE-PE Static Routes (huongdan dòng 17) ────────────────────────────────
    ce1.cmd('ip route del default 2>/dev/null')
    ce1.r('default', '172.16.1.2')       # CE1 → PE1

    ce2.cmd('ip route del default 2>/dev/null')
    ce2.r('default', '172.16.2.2')       # CE2 → PE2

    ce3.cmd('ip route del default 2>/dev/null')
    ce3.r('default', '172.16.3.2')       # CE3 → PE3
    # B3 routes trên CE3 được học từ OSPF Branch 3 (không cần static)

    info('    Static routes configured (CE-PE + Customer Prefixes).\n')


# ─────────────────────────────────────────────────────────────────────────────
# [6] VLAN 802.1Q BRANCH 2  (Core-Distribution-Access)
# ─────────────────────────────────────────────────────────────────────────────
def setup_branch2_vlan(net):
    """
    Cấu hình 802.1Q VLAN cho Branch 2 (3-Layer Architecture):
      VLAN 10 → Admin  (10.2.10.0/24)  – b2a1: b2a1-adm1, b2a1-adm2 (access)
      VLAN 20 → Lab    (10.2.20.0/24)  – b2a2: b2a2-lab1, b2a2-lab2  (access)
      VLAN 30 → Guest  (10.2.30.0/24)  – b2a3: b2a3-gst1, b2a3-gst2  (access)

    Tất cả uplink giữa Core↔Dist↔Access và CE2↔Core đều là TRUNK (VLAN 10,20,30).
    CE2 thực hiện inter-VLAN routing qua subinterface ce2-c1.10/20/30.
    """
    def set_trunk(node, ports, vlans='10,20,30'):
        for p in ports:
            node.cmd(f'ovs-vsctl set port {p} trunks={vlans} tag=[] 2>/dev/null; true')

    def set_access(node, ports, vlan):
        for p in ports:
            node.cmd(f'ovs-vsctl set port {p} tag={vlan} trunks=[] 2>/dev/null; true')

    b2c1 = net['b2c1']; b2c2 = net['b2c2']
    b2d1 = net['b2d1']; b2d2 = net['b2d2']
    b2a1 = net['b2a1']; b2a2 = net['b2a2']; b2a3 = net['b2a3']

    # Core switches: tất cả ports đều là trunk (kết nối CE2 + distribution)
    set_trunk(b2c1, ['b2c1-ce2', 'b2c1-d1', 'b2c1-d2'])
    set_trunk(b2c2, ['b2c2-ce2', 'b2c2-d1', 'b2c2-d2'])

    # Distribution switches: trunk lên core, trunk xuống access
    set_trunk(b2d1, ['b2d1-c1', 'b2d1-c2', 'b2d1-a1', 'b2d1-a2', 'b2d1-a3'])
    set_trunk(b2d2, ['b2d2-c1', 'b2d2-c2', 'b2d2-a1', 'b2d2-a2', 'b2d2-a3'])

    # Access switch b2a1 (Admin – VLAN 10)
    set_trunk(b2a1,  ['b2a1-d1', 'b2a1-d2'], vlans='10')   # uplink trunk VLAN10
    set_access(b2a1, ['b2a1-adm1', 'b2a1-adm2'], vlan=10)  # host ports access

    # Access switch b2a2 (Lab – VLAN 20)
    set_trunk(b2a2,  ['b2a2-d1', 'b2a2-d2'], vlans='20')
    set_access(b2a2, ['b2a2-lab1', 'b2a2-lab2'], vlan=20)

    # Access switch b2a3 (Guest – VLAN 30)
    set_trunk(b2a3,  ['b2a3-d1', 'b2a3-d2'], vlans='30')
    set_access(b2a3, ['b2a3-gst1', 'b2a3-gst2'], vlan=30)

    info('    Branch 2 VLAN configured: VLAN10=admin, VLAN20=lab, VLAN30=guest\n')
    info('    CE2 inter-VLAN routing: ce2-c1.10(10.2.10.254) .20(20.254) .30(30.254)\n')


# ─────────────────────────────────────────────────────────────────────────────
# [8] VPLS – VIRTUAL PRIVATE LAN SERVICE  (huongdan.txt dòng 21-23)
# ─────────────────────────────────────────────────────────────────────────────
def setup_vpls(net):
    """
    VPLS giả lập bằng Linux Bridge + GRE Pseudowire (huongdan dòng 21-23):
      - Mỗi PE tạo một Bridge-group (br-vpls) đóng vai trò VPLS instance.
      - GRE tunnel giữa các PE đóng vai trò Pseudowire (đường hầm Layer 2).
      - GRE encap dùng loopback IP → gói tin đi qua MPLS backbone (LDP labels).
      - Gán management IP 172.31.0.x/24 lên bridge để kiểm tra VPLS hoạt động.

    Kiểm tra sau khi setup:
      mininet> pe1 ping -c 3 172.31.0.2   → VPLS PE1↔PE2 (qua GRE/MPLS)
      mininet> pe1 ping -c 3 172.31.0.3   → VPLS PE1↔PE3 (qua GRE/MPLS)
    """
    vpls_peers = {
        'pe1': ('10.0.0.1', '172.31.0.1', [('pw_pe1_pe2', '10.0.0.2'),
                                            ('pw_pe1_pe3', '10.0.0.3')]),
        'pe2': ('10.0.0.2', '172.31.0.2', [('pw_pe2_pe1', '10.0.0.1'),
                                            ('pw_pe2_pe3', '10.0.0.3')]),
        'pe3': ('10.0.0.3', '172.31.0.3', [('pw_pe3_pe1', '10.0.0.1'),
                                            ('pw_pe3_pe2', '10.0.0.2')]),
    }

    for pe_name, (local_lo, vpls_ip, peers) in vpls_peers.items():
        pe = net[pe_name]

        # Tạo VPLS Bridge-group
        pe.cmd('ip link add br-vpls type bridge 2>/dev/null || true')
        pe.cmd('ip link set br-vpls up')
        pe.cmd(f'ip addr add {vpls_ip}/24 dev br-vpls 2>/dev/null || true')

        # Tạo GRE Pseudowire đến từng PE láng giềng và đưa vào bridge
        # Dùng "gretap" (Ethernet-in-GRE, L2) thay vì "gre" (IP-in-GRE, L3).
        # gretap tạo interface Ethernet thực sự → carry full Ethernet frame kể cả ARP.
        # gre L3 chỉ carry IP payload, ARP không qua được → "Destination Host Unreachable"
        for pw_name, remote_lo in peers:
            pe.cmd(f'ip link del {pw_name} 2>/dev/null; true')
            pe.cmd(
                f'ip link add {pw_name} type gretap '
                f'local {local_lo} remote {remote_lo} ttl 255 '
                f'2>/dev/null || true'
            )
            pe.cmd(f'ip link set {pw_name} up')
            pe.cmd(f'ip link set {pw_name} master br-vpls')


        info(f'    {pe_name}: VPLS Bridge (br-vpls={vpls_ip}) + '
             f'{len(peers)} GRE pseudowires created\n')

    info('    VPLS ready. Test: pe1 ping -c 3 172.31.0.2\n')

    # ── Fix: ghi đè LDP MPLS encap route cho PE loopback bằng plain IP ──────
    # Khi LDP converge, nó install route dạng:
    #   10.0.0.2/32 encap mpls <label> via ... proto ldp (admin dist 9)
    # Route này conflict với GRE tunnel khiến ping fail.
    # Dùng 'ip route replace' để tạo kernel route (proto static, metric 1)
    # mà FRR xử lý như admin distance 0 → LDP không override được.
    fix_routes = {
        'pe1': [('10.0.0.2', '10.100.101.2'), ('10.0.0.3', '10.100.103.2')],
        'pe2': [('10.0.0.1', '10.100.203.2'), ('10.0.0.3', '10.100.204.2')],
        'pe3': [('10.0.0.1', '10.50.32.2'),   ('10.0.0.2', '10.50.34.2')],
    }
    for pe_name, routes in fix_routes.items():
        pe = net[pe_name]
        for dst_lo, via_ip in routes:
            pe.cmd(f'ip route replace {dst_lo}/32 via {via_ip} metric 1 2>/dev/null; true')
    info('    VPLS route override applied (plain IP for GRE endpoints).\n')

    # ── Permanent ARP để loại bỏ packet loss do ARP cache expire ─────────────
    # Vấn đề: gretap cần ARP để biết MAC của remote PE trên bridge.
    # ARP default expire sau 30-60s → packet đầu tiên bị drop mỗi lần expire.
    # Fix: đọc MAC của br-vpls từ mỗi PE và thêm vào bảng neighbor là "permanent"
    # → ARP không bao giờ expire, ping luôn pass ngay từ packet đầu tiên.
    vpls_ip_map = {
        'pe1': '172.31.0.1',
        'pe2': '172.31.0.2',
        'pe3': '172.31.0.3',
    }
    br_macs = {}
    for pn in ['pe1', 'pe2', 'pe3']:
        mac_raw = net[pn].cmd(
            "ip link show br-vpls | awk '/link\\/ether/{print $2}'"
        ).strip()
        # Lọc dòng output sạch (lấy dòng đầu tiên nếu có nhiều dòng)
        mac = mac_raw.split('\n')[0].strip() if mac_raw else ''
        br_macs[pn] = mac

    for pn in ['pe1', 'pe2', 'pe3']:
        pe = net[pn]
        for remote_pn, remote_ip in vpls_ip_map.items():
            if remote_pn == pn:
                continue
            mac = br_macs.get(remote_pn, '')
            if mac and len(mac) == 17:   # kiểm tra định dạng MAC hợp lệ (xx:xx:xx:xx:xx:xx)
                pe.cmd(
                    f'ip neigh replace {remote_ip} dev br-vpls '
                    f'lladdr {mac} nud permanent 2>/dev/null; true'
                )
    info('    Permanent ARP entries seeded (no ARP learning delay).\n')

    # ── Permanent FDB để ngừng bridge flooding trong full-mesh ───────────────
    # Vấn đề: full-mesh GRETAP (PE1↔PE2, PE1↔PE3, PE2↔PE3) = triangle loop.
    # Khi bridge chưa biết MAC của remote PE → flood ra TẤT CẢ 2 ports →
    # mỗi remote PE nhận frame → flood tiếp → vòng lặp vô tận → mất packet.
    #
    # Fix: thêm static FDB entry (MAC → port cụ thể) vào bridge FDB:
    #   PE1: frame cho MAC(PE2) → luôn dùng pw_pe1_pe2  (không flood)
    #        frame cho MAC(PE3) → luôn dùng pw_pe1_pe3
    # → bridge BIẾT chính xác phải dùng port nào → không flood → không loop.
    lo_to_pe_name = {
        '10.0.0.1': 'pe1', '10.0.0.2': 'pe2', '10.0.0.3': 'pe3',
    }
    for pe_name, (local_lo, vpls_ip, peers) in vpls_peers.items():
        pe = net[pe_name]
        for pw_name, remote_lo in peers:
            remote_pe = lo_to_pe_name.get(remote_lo, '')
            remote_mac = br_macs.get(remote_pe, '')
            if remote_mac and len(remote_mac) == 17:
                pe.cmd(
                    f'bridge fdb replace {remote_mac} dev {pw_name} '
                    f'master permanent 2>/dev/null; true'
                )
    info('    Permanent FDB entries added (no bridge flooding/loop).\n')


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG / STATUS
# ─────────────────────────────────────────────────────────────────────────────
def debug_status(net):
    """
    In trạng thái OSPF neighbors, LDP bindings và MPLS routes.
    Dùng trong CLI: mininet> py debug_status(net)
    """
    backbone_nodes = ['pe1', 'pe2', 'pe3', 'p1', 'p2']
    for name in backbone_nodes:
        node = net[name]
        print(f'\n{"="*50}')
        print(f'  {name.upper()} – OSPF + LDP Status')
        print('='*50)

        try:
            # OSPF neighbors via nc TCP VTY (namespace-correct)
            print('[OSPF Neighbors]')
            tmpf = f'/tmp/frr_{name}_ospf_show.txt'
            with open(tmpf, 'w') as f:
                f.write('show ip ospf neighbor\nquit\n')
            out = node.cmd(
                f'nc -w 3 127.0.0.1 {OSPFD_PORT} < {tmpf} 2>/dev/null'
                f' | strings | grep -E "(Full|2-Way|Exch|Init|Down)" | head -10'
            )
            print(out.strip() or '  (no OSPF neighbors / still converging)')

            # LDP neighbors via nc TCP VTY
            print('\n[LDP Neighbors]')
            tmpf2 = f'/tmp/frr_{name}_ldp_nbr.txt'
            with open(tmpf2, 'w') as f:
                f.write('show mpls ldp neighbor\nquit\n')
            ldp_nbr = node.cmd(
                f'nc -w 3 127.0.0.1 {LDPD_PORT} < {tmpf2} 2>/dev/null | strings | grep -v "^$" | head -10'
            )
            print(ldp_nbr.strip() or '  (no LDP neighbors yet)')

            # LDP bindings summary via nc TCP VTY
            print('\n[LDP Bindings (top 8)]')
            tmpf3 = f'/tmp/frr_{name}_ldp_bind.txt'
            with open(tmpf3, 'w') as f:
                f.write('show mpls ldp binding\nquit\n')
            ldp_bind = node.cmd(
                f'nc -w 3 127.0.0.1 {LDPD_PORT} < {tmpf3} 2>/dev/null'
                f' | strings | grep -E "10\\.(1|2|3)\\." | head -8'
            )
            print(ldp_bind.strip() or '  (no LDP bindings / use: ip -f mpls route show)')

            # Kernel MPLS table (ground truth)
            print('\n[Kernel MPLS table (proto ldp)]')
            print(node.cmd('ip -f mpls route show 2>/dev/null | head -8').strip()
                  or '  (no LDP labels yet)')

            # VPLS bridge status
            print('\n[VPLS Bridge]')
            print(node.cmd('ip link show br-vpls 2>/dev/null | head -2').strip()
                  or '  (br-vpls not found)')

            # IP routes summary
            print('\n[IP Routes (customer prefixes)]')
            print(node.cmd(
                'ip route show | grep -E "10\\.1\\.|10\\.2\\.|10\\.3\\." | head -8'
            ).strip() or '  (no customer routes)')

        except Exception as e:
            print(f'  [ERROR] {e}')


def show_ldp(net, node_name='p1'):
    """
    Hiển thị LDP neighbor + binding cho một node qua TCP VTY.
    Dùng thay thế cho 'vtysh' (vtysh dùng Unix socket bị share giữa nodes).

    Cách dùng trong CLI:
      mininet> py show_ldp(net, 'p1')    # LDP status của p1
      mininet> py show_ldp(net, 'pe1')   # LDP status của pe1
    """
    node = net[node_name]
    print(f'\n=== LDP Status: {node_name.upper()} (via TCP VTY port {LDPD_PORT}) ===')

    for label, cmd_body in [
        ('LDP Neighbors', 'show mpls ldp neighbor\n'),
        ('LDP Bindings',  'show mpls ldp binding\n'),
        ('LDP Interfaces','show mpls ldp interface\n'),
    ]:
        tmpf = f'/tmp/frr_{node_name}_ldp_{label.split()[1].lower()}.txt'
        with open(tmpf, 'w') as f:
            f.write(cmd_body + 'quit\n')
        out = node.cmd(f'nc -w 3 127.0.0.1 {LDPD_PORT} < {tmpf} 2>/dev/null | strings')
        print(f'\n[{label}]')
        print(out.strip() or f'  (empty - ldpd may still be converging)')

    print('\n[Kernel MPLS table]')
    print(node.cmd('ip -f mpls route show 2>/dev/null').strip() or '  (empty)')



# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def print_summary():
    info('\n')
    info('╔══════════════════════════════════════════════════════════╗\n')
    info('║   METRO ETHERNET MPLS v5 – FRR OSPF + LDP + VPLS        ║\n')
    info('╠══════════════════════════════════════════════════════════╣\n')
    info('║  Backbone OSPF:   PE1(10.0.0.1) PE2(10.0.0.2) PE3(10.0.0.3) ║\n')
    info('║  P-Routers:       P1(11) P2(12) P3(13) P4(14) Full Mesh  ║\n')
    info('║  LDP Transport:   Loopback IPs (auto label distribution)  ║\n')
    info('╠══════════════════════════════════════════════════════════╣\n')
    info('║  Branch 1 (Flat)        h1-h4    10.1.0.0/24             ║\n')
    info('║  Branch 2 (3-Layer)     admin    10.2.10.0/24            ║\n')
    info('║                         lab      10.2.20.0/24            ║\n')
    info('║                         guest    10.2.30.0/24            ║\n')
    info('║  Branch 3 (Leaf-Spine)  web      10.3.10.0/24            ║\n')
    info('║                         dns      10.3.20.0/24            ║\n')
    info('║                         db       10.3.30.0/24            ║\n')
    info('╠══════════════════════════════════════════════════════════╣\n')
    info('║  VPLS Segment: PE1(172.31.0.1) PE2(172.31.0.2) PE3(172.31.0.3) ║\n')
    info('╠══════════════════════════════════════════════════════════╣\n')
    info('║  Lệnh kiểm tra:                                          ║\n')
    info('║   h1 ping -c 3 10.3.10.1         → B1→B3 (LDP labels)   ║\n')
    info('║   pe1 ping -c 3 172.31.0.2        → VPLS pseudowire test ║\n')
    info('║   p1 ip -f mpls route show        → Kernel MPLS table    ║\n')
    info("║   py show_ldp(net,'p1')           → LDP nbr+binding      ║\n")
    info('║   py debug_status(net)            → Full OSPF/LDP status ║\n')
    info('║   py quick_test(net)              → Connectivity test     ║\n')
    info('╚══════════════════════════════════════════════════════════╝\n')
    info('\n')


# ─────────────────────────────────────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────────────────────────────────────
def quick_test(net):
    info('\n*** === QUICK CONNECTIVITY TEST ===\n')
    tests = [
        ('h1',     '10.1.0.2',    'h1 → h2        (intra B1 Flat)'),
        ('h1',     '10.2.10.1',   'h1 → admin1    (B1 → B2, MPLS)'),
        ('h1',     '10.3.10.1',   'h1 → web1      (B1 → B3, MPLS)'),
        ('admin1', '10.3.20.1',   'admin1 → dns1  (B2 → B3, MPLS)'),
        ('web1',   '10.2.30.1',   'web1 → guest1  (B3 → B2, MPLS)'),
        ('db1',    '10.1.0.1',    'db1 → h1       (B3 → B1, MPLS)'),
        ('pe1',    '172.31.0.2',  'pe1 → pe2      (VPLS Pseudowire)'),
        ('pe1',    '172.31.0.3',  'pe1 → pe3      (VPLS Pseudowire)'),
    ]
    passed = 0
    for src, dst, desc in tests:
        out = net[src].cmd(f'ping -c 3 -W 2 {dst}')
        ok  = '0% packet loss' in out
        mark = '✓' if ok else '✗'
        info(f'  [{mark}] {desc}  ({dst})\n')
        if ok:
            passed += 1
    info(f'\n  Kết quả: {passed}/{len(tests)} tests pass\n')
    return passed == len(tests)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    setLogLevel('info')

    if '--clean' in sys.argv or '-c' in sys.argv:
        cleanup()
        sys.exit(0)

    if os.geteuid() != 0:
        print('Cần sudo: sudo python3 topology_frr.py')
        sys.exit(1)

    net = build_net()
    try:
        if '--test' in sys.argv:
            quick_test(net)
            time.sleep(5)
        elif '--status' in sys.argv:
            debug_status(net)
            time.sleep(5)
        else:
            quick_test(net)
            import builtins
            builtins.debug_status = debug_status
            builtins.quick_test   = quick_test
            builtins.show_ldp     = show_ldp
            CLI(net)
    finally:
        remove_netns_symlinks(net)
        net.stop()
        cleanup()

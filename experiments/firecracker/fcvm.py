#!/usr/bin/env python3
"""Minimal per-run microVM driver: one Firecracker process per run.

Subcommands
  net up|down N          per-VM tap + /30 + NAT (host 172.30.N.1, guest 172.30.N.2)
  boot N [opts]          write a config file, exec firecracker, wait for sshd
  ssh N -- argv...       run a command in the guest, streamed
  snapshot N <dir>       pause, snapshot, resume (or --kill)
  restore N <dir> [opts] restore a snapshot into a fresh VM
  kill N                 SIGKILL the firecracker process
  stat N                 host RSS of the firecracker process

Everything a run needs is a file on the host: the config, the disks, the log,
the API socket. There is no daemon and no shared state between runs, which is
the whole argument for the design.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

SPIKE = os.environ.get("SPIKE", os.path.expanduser("~/spike-fc"))
FC = f"{SPIKE}/bin/firecracker"
RUN = f"{SPIKE}/run"
IMG = f"{SPIKE}/img"
KEY = os.path.expanduser("~/.ssh/fcspike")
UPLINK = os.environ.get("UPLINK", "ens3")


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, **kw)


def guest_ip(n):
    return f"172.30.{n}.2"


def host_ip(n):
    return f"172.30.{n}.1"


def tap(n):
    return f"fctap{n}"


def net_up(n):
    t = tap(n)
    sh(f"sudo ip link del {t}", check=False, stderr=subprocess.DEVNULL)
    sh(f"sudo ip tuntap add {t} mode tap")
    sh(f"sudo ip addr add {host_ip(n)}/30 dev {t}")
    sh(f"sudo ip link set {t} up")
    sh("sudo sysctl -qw net.ipv4.ip_forward=1")
    # additive, removable rules; nothing about the host dockerd changes
    sh(f"sudo iptables -t nat -C POSTROUTING -s 172.30.{n}.0/30 -o {UPLINK} -j MASQUERADE 2>/dev/null"
       f" || sudo iptables -t nat -A POSTROUTING -s 172.30.{n}.0/30 -o {UPLINK} -j MASQUERADE")
    sh(f"sudo iptables -C FORWARD -i {t} -o {UPLINK} -j ACCEPT 2>/dev/null"
       f" || sudo iptables -I FORWARD 1 -i {t} -o {UPLINK} -j ACCEPT")
    sh(f"sudo iptables -C FORWARD -o {t} -i {UPLINK} -j ACCEPT 2>/dev/null"
       f" || sudo iptables -I FORWARD 1 -o {t} -i {UPLINK} -j ACCEPT")


def net_down(n):
    t = tap(n)
    sh(f"sudo iptables -t nat -D POSTROUTING -s 172.30.{n}.0/30 -o {UPLINK} -j MASQUERADE", check=False,
       stderr=subprocess.DEVNULL)
    sh(f"sudo iptables -D FORWARD -i {t} -o {UPLINK} -j ACCEPT", check=False, stderr=subprocess.DEVNULL)
    sh(f"sudo iptables -D FORWARD -o {t} -i {UPLINK} -j ACCEPT", check=False, stderr=subprocess.DEVNULL)
    sh(f"sudo ip link del {t}", check=False, stderr=subprocess.DEVNULL)


def mac(n):
    return f"06:00:ac:1e:{n:02x}:02"


def write_config(n, args):
    d = f"{RUN}/vm{n}"
    os.makedirs(d, exist_ok=True)
    boot_args = (
        "console=ttyS0 reboot=k panic=1 pci=off net.ifnames=0 "
        "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd "
        "root=/dev/vda rw init=/lib/systemd/systemd "
        f"fcnet={guest_ip(n)}/30,{host_ip(n)},vm{n}"
    )
    drives = [{
        "drive_id": "rootfs", "path_on_host": args.rootfs,
        "is_root_device": True, "is_read_only": False,
    }]
    # vdb scratch/workspace, vdc read-only warm base, vdd outputs
    for did, path, ro in (("workspace", args.workspace, False),
                          ("warmbase", args.warmbase, True),
                          ("outputs", args.outputs, False)):
        if path:
            drives.append({"drive_id": did, "path_on_host": path,
                           "is_root_device": False, "is_read_only": ro})
    cfg = {
        "boot-source": {"kernel_image_path": args.kernel, "boot_args": boot_args},
        "drives": drives,
        "machine-config": {"vcpu_count": args.vcpus, "mem_size_mib": args.mem,
                           "track_dirty_pages": bool(args.track_dirty)},
        "network-interfaces": [{"iface_id": "eth0", "host_dev_name": tap(n),
                                "guest_mac": mac(n)}],
    }
    if args.balloon:
        cfg["balloon"] = {"amount_mib": args.balloon, "deflate_on_oom": True,
                          "stats_polling_interval_s": 1}
    p = f"{d}/config.json"
    json.dump(cfg, open(p, "w"), indent=2)
    return d, p


def wait_ssh(n, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection((guest_ip(n), 22), 0.5)
            s.close()
            return time.time() - t0
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"no sshd on {guest_ip(n)} after {timeout}s")


def boot(n, args):
    d, cfgp = write_config(n, args)
    api = f"{d}/api.sock"
    for f in (api,):
        if os.path.exists(f):
            os.unlink(f)
    log = open(f"{d}/console.log", "wb")
    t0 = time.time()
    p = subprocess.Popen([FC, "--api-sock", api, "--config-file", cfgp],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    open(f"{d}/pid", "w").write(str(p.pid))
    dt = wait_ssh(n, args.timeout)
    print(json.dumps({"vm": n, "pid": p.pid, "ssh_ready_s": round(dt, 3),
                      "boot_to_ssh_s": round(time.time() - t0, 3)}))
    return p.pid


SSH_OPTS = ["-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15"]


def gssh(n, argv, capture=False):
    cmd = ["ssh", *SSH_OPTS, f"root@{guest_ip(n)}", *argv]
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    return subprocess.call(cmd)


def api(n, method, path, body=None):
    d = f"{RUN}/vm{n}"
    cmd = ["curl", "-s", "--unix-socket", f"{d}/api.sock", "-X", method,
           f"http://localhost{path}", "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout


def rss_kib(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    n_ = sub.add_parser("net"); n_.add_argument("dir", choices=["up", "down"]); n_.add_argument("n", type=int)

    b = sub.add_parser("boot")
    b.add_argument("n", type=int)
    b.add_argument("--kernel", default=f"{IMG}/vmlinux-" + os.uname().release)
    b.add_argument("--rootfs", default=f"{IMG}/rootfs.ext4")
    b.add_argument("--workspace"); b.add_argument("--warmbase"); b.add_argument("--outputs")
    b.add_argument("--vcpus", type=int, default=2)
    b.add_argument("--mem", type=int, default=4096)
    b.add_argument("--balloon", type=int, default=0)
    b.add_argument("--track-dirty", type=int, default=0)
    b.add_argument("--timeout", type=int, default=120)

    s = sub.add_parser("ssh"); s.add_argument("n", type=int); s.add_argument("argv", nargs=argparse.REMAINDER)
    k = sub.add_parser("kill"); k.add_argument("n", type=int)
    st = sub.add_parser("stat"); st.add_argument("n", type=int)

    sn = sub.add_parser("snapshot"); sn.add_argument("n", type=int); sn.add_argument("dir")
    sn.add_argument("--resume", action="store_true")
    sn.add_argument("--diff", action="store_true")

    rs = sub.add_parser("restore"); rs.add_argument("n", type=int); rs.add_argument("dir")
    rs.add_argument("--rootfs"); rs.add_argument("--timeout", type=int, default=60)

    a = ap.parse_args()
    if a.cmd == "net":
        (net_up if a.dir == "up" else net_down)(a.n)
    elif a.cmd == "boot":
        boot(a.n, a)
    elif a.cmd == "ssh":
        argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
        sys.exit(gssh(a.n, argv))
    elif a.cmd == "kill":
        pid = int(open(f"{RUN}/vm{a.n}/pid").read())
        os.kill(pid, signal.SIGKILL)
        print(json.dumps({"killed": pid}))
    elif a.cmd == "stat":
        pid = int(open(f"{RUN}/vm{a.n}/pid").read())
        print(json.dumps({"pid": pid, "rss_kib": rss_kib(pid)}))
    elif a.cmd == "snapshot":
        os.makedirs(a.dir, exist_ok=True)
        t0 = time.time()
        print(api(a.n, "PATCH", "/vm", {"state": "Paused"}))
        tp = time.time()
        body = {"snapshot_path": f"{a.dir}/snap.file", "mem_file_path": f"{a.dir}/mem.file",
                "snapshot_type": "Diff" if a.diff else "Full"}
        print(api(a.n, "PUT", "/snapshot/create", body))
        tc = time.time()
        if a.resume:
            print(api(a.n, "PATCH", "/vm", {"state": "Resumed"}))
        print(json.dumps({"pause_s": round(tp - t0, 3), "create_s": round(tc - tp, 3),
                          "mem_bytes": os.path.getsize(f"{a.dir}/mem.file"),
                          "snap_bytes": os.path.getsize(f"{a.dir}/snap.file")}))
    elif a.cmd == "restore":
        d = f"{RUN}/vm{a.n}"
        os.makedirs(d, exist_ok=True)
        apis = f"{d}/api.sock"
        if os.path.exists(apis):
            os.unlink(apis)
        log = open(f"{d}/console.log", "wb")
        t0 = time.time()
        p = subprocess.Popen([FC, "--api-sock", apis], stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        open(f"{d}/pid", "w").write(str(p.pid))
        for _ in range(200):
            if os.path.exists(apis):
                break
            time.sleep(0.01)
        body = {"snapshot_path": f"{a.dir}/snap.file",
                "mem_backend": {"backend_type": "File", "backend_path": f"{a.dir}/mem.file"},
                "enable_diff_snapshots": False, "resume_vm": True,
                "network_overrides": [{"iface_id": "eth0", "host_dev_name": tap(a.n)}]}
        out = api(a.n, "PUT", "/snapshot/load", body)
        tl = time.time()
        if out.strip():
            print(out)
        try:
            dt = wait_ssh(a.n, a.timeout)
        except TimeoutError as e:
            print(json.dumps({"error": str(e), "load_s": round(tl - t0, 3)}))
            raise SystemExit(1)
        print(json.dumps({"vm": a.n, "pid": p.pid, "load_s": round(tl - t0, 3),
                          "restore_to_ssh_s": round(time.time() - t0, 3)}))


if __name__ == "__main__":
    main()

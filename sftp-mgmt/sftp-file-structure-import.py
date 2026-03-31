import paramiko
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import json
import threading
import socks
from pathlib import PurePosixPath

# ============= CONFIGURATION =============
SFTP_HOST = ""
SFTP_PORT = 10104
SFTP_USER = "fnoel@sqorus.com"
SFTP_PASSWORD = None  # Or None if using an SSH key
SFTP_KEY_PATH = r"C:\Users\fnoel\.ssh\id_rsa_main"  
SFTP_KEY_TYPE = "rsa"  # "rsa"
# SOCKS5 proxy (optional)
USE_SOCKS5_PROXY = False
PROXY_HOST = "xxx"
PROXY_PORT = 1080
PROXY_RDNS = True  # DNS resolution via proxy
# SOURCE CONFIGURATION (exported JSON)
INPUT_PATH  = "sftp_file_structure.json"

ROOT_OVERRIDE = None  # Overrides the root
# Workers
MAX_WORKERS = 8  # Number of parallel SFTP connections (adjust depending on server)
MAX_DEPTH = 1  # Maximum depth to scan (1 = root)
# =========================================
# SFTP connection pool (one per thread)
_thread_local = threading.local()
_connections: list = []
_connections_lock = Lock()

def _make_ssh_client() -> paramiko.SFTPClient:
    """Creates a new SFTP connection."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if USE_SOCKS5_PROXY:
        sock = socks.create_connection(
            (SFTP_HOST,SFTP_PORT),
            proxy_type=socks.SOCKS5,
            proxy_addr=PROXY_HOST,
            proxy_port=PROXY_PORT,
            proxy_rdns=True,
        )
        if SFTP_KEY_PATH:
            if SFTP_KEY_TYPE.lower() == "ed25519":
                key = paramiko.Ed25519Key.from_private_key_file(SFTP_KEY_PATH)
            elif SFTP_KEY_TYPE.lower() == "rsa":
                key = paramiko.RSAKey.from_private_key_file(SFTP_KEY_PATH)
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, pkey=key, sock=sock)
        else:
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASSWORD, sock=sock)
    else:
        if SFTP_KEY_PATH:
            if SFTP_KEY_TYPE.lower() == "ed25519":
                key = paramiko.Ed25519Key.from_private_key_file(SFTP_KEY_PATH)
            elif SFTP_KEY_TYPE.lower() == "rsa":
                key = paramiko.RSAKey.from_private_key_file(SFTP_KEY_PATH)
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, pkey=key)
        else:
            ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASSWORD)
    return ssh.open_sftp()

def get_sftp() -> paramiko.SFTPClient:
    """Returns the SFTP connection for the current thread (creates if not exists)."""
    if not getattr(_thread_local, "sftp", None):
        sftp = _make_ssh_client()
        _thread_local.sftp = sftp
        with _connections_lock:
            _connections.append(sftp)
    return _thread_local.sftp

def close_all():
    """Closes all connections in the pool."""
    with _connections_lock:
        for sftp in _connections:
            try:
                sftp.close()
            except Exception:
                pass
        _connections.clear()

def mkdir_p(sftp: paramiko.SFTPClient, path: str):
    """Creates a directory and all its parents if necessary (mkdir -p)."""
    parts = PurePosixPath(path).parts
    current = ""
    for part in parts:
        current = str(PurePosixPath(current) / part) if current else part
        if not current or current == "/":
            continue
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
                print(f"  📁 Created: {current}")
            except OSError:
                # May occur due to race condition between threads — ignore
                pass

def collect_all_paths(node: dict, src_root: str, dst_root: str) -> list[str]:
    """
    Recursively traverses the JSON tree and returns the list of all
    destination paths to create.
    """
    paths = []

    def _walk(n: dict):
        src_path = n["path"]
        # Replace source root with destination root
        rel = PurePosixPath(src_path).relative_to(src_root)
        dst_path = str(PurePosixPath(dst_root) / rel)
        paths.append(dst_path)
        for child in n.get("children", []):
            _walk(child)

    for child in node.get("tree", []):
        _walk(child)
    return paths

def create_directories_parallel(paths: list[str], max_workers: int = MAX_WORKERS):
    """
    Creates all directories in parallel.
    Paths are sorted by depth to avoid parent/child conflicts.
    """
    # Sort by ascending depth — parents are created before children
    sorted_paths = sorted(paths, key=lambda p: p.count("/"))

    total = len(sorted_paths)
    done = [0]
    done_lock = Lock()

    def worker(path: str):
        sftp = get_sftp()
        mkdir_p(sftp, path)
        with done_lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == total:
                print(f"  ⏳ Progress: {done[0]}/{total} directories")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(worker, p) for p in sorted_paths]
        for f in as_completed(futures):
            f.result()

def main():
    try:
        with open(INPUT_PATH, "r", encoding="utf-8") as f:
            export = json.load(f)

        src_root = export["root"]
        dst_root = ROOT_OVERRIDE if ROOT_OVERRIDE else src_root

        print(f"📂 Source root      : {src_root}")
        print(f"📂 Destination root : {dst_root}")

        # Collect all paths to create
        print("\n🔍 Analyzing JSON...")
        paths = collect_all_paths(export, src_root, dst_root)
        print(f"✅ {len(paths)} directories to create")

        if not paths:
            print("⚠️  No directories found in JSON.")
            return

        # Initial connection
        print("\n🔌 Connecting to SFTP server")
        get_sftp()
        print("✅ Connected!")

        # Create destination root if necessary
        sftp = get_sftp()
        mkdir_p(sftp, dst_root)

        # Parallel creation
        print(f"\n⚡ Creating in parallel ({MAX_WORKERS} workers)...")
        create_directories_parallel(paths)

        close_all()
        print(f"\n✅ Directory tree recreated on {SFTP_HOST}:{dst_root}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

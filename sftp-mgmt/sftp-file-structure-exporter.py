import paramiko
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import json
import threading

# ============= CONFIGURATION =============
SFTP_HOST = "79.76.125.51"
SFTP_PORT = 22
SFTP_USER = "ubuntu"
SFTP_PASSWORD = None  # Ou None si vous utilisez une clé SSH
SFTP_KEY_PATH = r"C:\Users\fnoel\.ssh\id_ed25519_main"  # "/chemin/vers/cle_ssh" ou None si vous utilisez un mot de passe

REMOTE_PATH = "/home/ubuntu"  # Dossier à analyser

# Proxy SOCKS5 (optionnel)
USE_SOCKS5_PROXY = False
PROXY_HOST = "proxy_host"
PROXY_PORT = 1080

OUTPUT_PATH = "sftp_file_structure.json"

MAX_WORKERS = 8  # Nombre de connexions SFTP parallèles (ajuster selon le serveur)

# =========================================

# Pool de connexions SFTP (une par thread)
_thread_local = threading.local()
_connections: list = []
_connections_lock = Lock()
 
 
def _make_ssh_client() -> paramiko.SFTPClient:
    """Crée une nouvelle connexion SFTP."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if SFTP_KEY_PATH:
        key = paramiko.Ed25519Key.from_private_key_file(SFTP_KEY_PATH)
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, pkey=key)
    else:
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASSWORD)
    return ssh.open_sftp()
 
 
def get_sftp() -> paramiko.SFTPClient:
    """Retourne la connexion SFTP du thread courant (crée si inexistante)."""
    if not getattr(_thread_local, "sftp", None):
        sftp = _make_ssh_client()
        _thread_local.sftp = sftp
        with _connections_lock:
            _connections.append(sftp)
    return _thread_local.sftp

def connect_sftp():
    """Connect to SFTP server"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    # Connect with password or SSH key
    if SFTP_KEY_PATH:
        private_key = paramiko.Ed25519Key.from_private_key_file(SFTP_KEY_PATH)
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, pkey=private_key)
    else:
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASSWORD)
    
    return ssh.open_sftp()

def connect_sftp_via_socks():
    socks.set_default_proxy(
        socks.SOCKS5,
        PROXY_HOST,
        PROXY_PORT
    )
    socket.socket = socks.socksocket

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key = paramiko.Ed25519Key.from_private_key_file(SFTP_KEY_PATH)
    ssh.connect(
        SFTP_HOST,
        port=22,
        username=SFTP_USER,
        pkey=key
    )

    return ssh.open_sftp()


def close_all():
    """Ferme toutes les connexions du pool."""
    with _connections_lock:
        for sftp in _connections:
            try:
                sftp.close()
            except Exception:
                pass
        _connections.clear()
 
 
def list_subdirs(path: str) -> list[dict]:
    """
    Liste les sous-dossiers immédiats de `path`.
    Retourne une liste de dicts {name, path} pour les entrées qui sont des dossiers.
    """
    sftp = get_sftp()
    results = []
    try:
        for entry in sftp.listdir_attr(path):
            entry_path = f"{path}/{entry.filename}".replace("//", "/")
            # stat.S_ISDIR vérifie le bit de mode — plus fiable que tenter listdir
            import stat
            if entry.st_mode and stat.S_ISDIR(entry.st_mode):
                results.append({"name": entry.filename, "path": entry_path})
    except IOError:
        pass
    return results

def scan_directory_parallel(root_path: str, max_workers: int = MAX_WORKERS) -> list:
    """
    Parcourt l'arborescence de dossiers en parallèle avec un pool de threads.
    Chaque thread maintient sa propre connexion SFTP.
    """
    # Chaque nœud est représenté par un dict mutable partagé
    root_node = {"name": root_path.split("/")[-1], "path": root_path, "children": []}
 
    # File de travail : (chemin_à_scanner, nœud_parent)
    pending = [(root_path, root_node)]
    pending_lock = Lock()
    counter_lock = Lock()
    active = [0]  # nombre de tâches en cours
 
    def worker(path: str, parent_node: dict):
        subdirs = list_subdirs(path)
        print(f"📂 {path} ({len(subdirs)} sous-dossiers)")
 
        nodes = []
        new_tasks = []
        for d in subdirs:
            node = {"name": d["name"], "path": d["path"], "children": []}
            nodes.append(node)
            new_tasks.append((d["path"], node))
 
        parent_node["children"] = nodes
 
        with pending_lock:
            pending.extend(new_tasks)
 
        with counter_lock:
            active[0] -= 1
 
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            with pending_lock:
                batch = pending[:]
                pending.clear()
 
            if not batch:
                with counter_lock:
                    if active[0] == 0:
                        break
                # Attendre que les tâches en cours ajoutent de nouveaux éléments
                import time
                time.sleep(0.05)
                continue
 
            with counter_lock:
                active[0] += len(batch)
 
            futures = [executor.submit(worker, path, node) for path, node in batch]
            # On attend les futures du batch courant avant de reboucler
            for f in as_completed(futures):
                f.result()  # propage les exceptions éventuelles
 
    return root_node["children"]

def main():
    print("🚀 Starting SFTP analysis...")
    print(f"📁 Server: {SFTP_HOST}")
    print(f"📂 Path: {REMOTE_PATH}")
    
    try:
        # Connection
        print("\n🔌 Connecting to SFTP server...")
        if USE_SOCKS5_PROXY:
            sftp = connect_sftp_via_socks()
        else:      
            sftp = connect_sftp()
        print("✅ Connected!")
        
        # Scan
        print("✅ Scan Starting!")
        tree = scan_directory_parallel(REMOTE_PATH)

        # Close
        sftp.close()
        print("\n✅ Scan completed!")

        export = {"root": REMOTE_PATH, "tree": tree}
        with open(OUTPUT_PATH, "w", encoding="utf-8") as output_file:
            json.dump(export, output_file, indent=2, ensure_ascii=False)
        print(f"✅ Exported to {OUTPUT_PATH}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
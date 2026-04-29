import json
import requests
import re
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ===================== CONFIGURATION =====================
MAX_WORKERS = 8          # Number of vouchers to check at once
THREAD_DELAY = 0.3       # Delay between starting threads
RETRY_DELAY = 0.5        # Delay before retrying a failed request
CHECK_INTERVAL = 60     # Seconds between cycles (1 minutes)

# Thread locks for safe file writing, proxy rotation, and counters
file_lock = threading.Lock()
proxy_lock = threading.Lock()
counter_lock = threading.Lock()

# Counters for summary
valid_count = 0
invalid_count = 0
error_count = 0

# ===================== TIMESTAMP HELPER =====================
def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ===================== PROXY MANAGER =====================
class ProxyRotator:
    def __init__(self, proxy_file="proxies.txt"):
        self.proxies = []
        self.index = 0
        self.load_proxies(proxy_file)

    def load_proxies(self, filename):
        """Load proxies from file, each line format: username:password@host:port"""
        if not os.path.exists(filename):
            print(f"⚠️ Proxy file {filename} not found. Running without proxies.")
            return
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '@' not in line:
                    continue
                try:
                    creds, hostport = line.split('@', 1)
                    if ':' in creds:
                        username, password = creds.split(':', 1)
                    else:
                        username = creds
                        password = ""
                    if ':' in hostport:
                        host, port = hostport.split(':', 1)
                    else:
                        host = hostport
                        port = "80"
                    proxy_url = f"http://{username}:{password}@{host}:{port}"
                    self.proxies.append({
                        "http": proxy_url,
                        "https": proxy_url
                    })
                except Exception as e:
                    print(f"⚠️ Skipping invalid proxy line '{line}': {e}")
        print(f"✅ Loaded {len(self.proxies)} proxies.")

    def get_next_proxy(self):
        """Return the next proxy dict for a new cycle, rotating round-robin."""
        with proxy_lock:
            if not self.proxies:
                return None
            proxy = self.proxies[self.index]
            self.index = (self.index + 1) % len(self.proxies)
            return proxy

# ===================== SHEIN LOGIC =====================
def load_cookies():
    if not os.path.exists("cookies.json"):
        print("❌ Error: cookies.json not found!")
        return None
    try:
        with open("cookies.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return "; ".join(f"{c['name']}={c['value']}" for c in data if "name" in c and "value" in c)
        return None
    except Exception as e:
        print(f"❌ Error loading cookies: {e}")
        return None

def check_voucher_task(code, cookie_string, session, proxy):
    """Check a voucher with exactly one retry on technical errors."""
    global valid_count, invalid_count, error_count

    url = "https://www.sheinindia.in/api/cart/apply-voucher"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-tenant-id": "SHEIN",
        "cookie": cookie_string
    }
    payload = {"voucherId": code, "device": {"client_type": "web"}}

    for attempt in range(2):  # attempt 0 = first try, 1 = retry
        try:
            r = session.post(url, json=payload, headers=headers, timeout=10, proxies=proxy)
            response = r.json()

            # Business response – no retry needed
            if "errorMessage" not in response:
                msg = f"✅ VALID: {code}"
                print(msg)
                with file_lock:
                    with open("applicable_vouchers.txt", "a", encoding="utf-8") as f:
                        # Write only the voucher code (no timestamp, no extra text)
                        f.write(f"{code}\n")
                with counter_lock:
                    valid_count += 1
                return
            else:
                error_msg = response.get("errorMessage", "Unknown error")
                msg = f"❌ NOT APPLICABLE: {code} - {error_msg}"
                print(msg)
                with file_lock:
                    with open("not_applicable_vouchers.txt", "a", encoding="utf-8") as f:
                        # Keep full details for debugging
                        f.write(f"{current_timestamp()} - {code} - {error_msg}\n")
                with counter_lock:
                    invalid_count += 1
                return

        except Exception as e:
            if attempt == 0:
                # First failure – wait and retry once
                print(f"🔄 Retry {code} after {type(e).__name__}")
                time.sleep(RETRY_DELAY)
                continue
            else:
                # Second failure – log error
                msg = f"⚠️ ERROR after retry: {code} - {type(e).__name__}: {e}"
                print(msg)
                with file_lock:
                    with open("error_vouchers.txt", "a", encoding="utf-8") as f:
                        f.write(f"{current_timestamp()} - {code} - {type(e).__name__}: {e}\n")
                with counter_lock:
                    error_count += 1
                return

def get_vouchers_from_file():
    if not os.path.exists("vouchers.txt"):
        return []
    with open("vouchers.txt", "r", encoding="utf-8") as f:
        content = f.read()
    codes = re.findall(r'\bS\w+', content)
    return list(dict.fromkeys(codes))

def write_run_separator():
    """Append a separator line to each output file to mark a new run."""
    timestamp = current_timestamp()
    separator = f"\n--- Run at {timestamp} ---\n"
    for filename in ["applicable_vouchers.txt", "not_applicable_vouchers.txt", "error_vouchers.txt"]:
        with file_lock:
            with open(filename, "a", encoding="utf-8") as f:
                f.write(separator)

# ===================== MAIN LOOP (AUTOMATIC EVERY 5 MIN) =====================
def run_one_cycle():
    global valid_count, invalid_count, error_count

    # Reset counters
    valid_count = invalid_count = error_count = 0

    # Write a run separator to all output files (append mode)
    write_run_separator()

    session = requests.Session()
    proxy_rotator = ProxyRotator()

    print(f"\n🚀 Starting check cycle (Workers: {MAX_WORKERS}, 1 retry)")
    print("Logging to: applicable_vouchers.txt (only codes), not_applicable_vouchers.txt, error_vouchers.txt (with timestamps)\n")

    cookie_string = load_cookies()
    codes = get_vouchers_from_file()

    if not cookie_string:
        print("❌ No valid cookies. Skipping cycle.")
        return
    if not codes:
        print("❌ No voucher codes found in vouchers.txt. Skipping cycle.")
        return

    proxy = proxy_rotator.get_next_proxy()
    if proxy:
        print(f"🌐 Using proxy: {proxy['http'].split('@')[1]}")
    else:
        print("⚠️ No proxy available – continuing without proxy.")

    print(f"Checking {len(codes)} codes...\n")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for code in codes:
            executor.submit(check_voucher_task, code, cookie_string, session, proxy)
            time.sleep(THREAD_DELAY)

    # Wait for all threads to finish (executor context manager does this)
    # Print summary
    print("\n" + "="*40)
    print("✅ CYCLE COMPLETE – SUMMARY")
    print(f"   Valid vouchers  : {valid_count}")
    print(f"   Invalid vouchers: {invalid_count}")
    print(f"   Errors          : {error_count}")
    print(f"   Total processed : {valid_count + invalid_count + error_count}")
    print("="*40)

def main_loop():
    cycle = 1
    try:
        # First cycle
        print(f"\n{'='*50}")
        print(f"🔁 CYCLE #{cycle} starting at {current_timestamp()}")
        print('='*50)
        run_one_cycle()

        # Ask user whether to continue after first cycle
        print()  # blank line
        answer = input("Continue indefinitely? (yes/no) [yes]: ").strip().lower()
        if answer in ('no', 'n'):
            print("🛑 Stopping by user request.")
            return

        # If user said yes, run forever (with interval) until Ctrl+C
        print("\n✅ Continuous mode activated. Will run every 5 minutes until you press Ctrl+C.\n")
        while True:
            # Wait for next cycle
            print(f"\n⏳ Waiting {CHECK_INTERVAL} seconds until next cycle (press Ctrl+C to stop)...")
            time.sleep(CHECK_INTERVAL)

            cycle += 1
            print(f"\n{'='*50}")
            print(f"🔁 CYCLE #{cycle} starting at {current_timestamp()}")
            print('='*50)
            run_one_cycle()

    except KeyboardInterrupt:
        print("\n🛑 Stopped by user (Ctrl+C).")

if __name__ == "__main__":
    main_loop()

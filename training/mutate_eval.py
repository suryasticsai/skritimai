import json, os, subprocess, random, tempfile, hashlib, requests, re, time
import concurrent.futures
import threading

API_KEY = os.environ.get("SKRITIMAI_KEY")
SKRITIMAI_URL = os.environ.get("SKRITIMAI_URL", "https://skritimai.varakala-saisurya.workers.dev/v1/chat/completions")
if not API_KEY:
    raise SystemExit("❌ Set SKRITIMAI_KEY environment variable")

OUTPUT_FILE = "dpo_training_data.jsonl"
INPUT_DIR = "clean_html"

file_lock = threading.Lock()

def extract_html(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    start = text.find('<!DOCTYPE')
    if start == -1: start = text.find('<html')
    end = text.rfind('</html>')
    if start != -1 and end != -1:
        return text[start:end+7]
    return text

def stream_request(prompt, max_retries=20):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "skritimai",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.7
    }
    
    attempts = 0
    while attempts < max_retries:
        full_text = ""
        try:
            with requests.post(SKRITIMAI_URL, headers=headers, json=payload, stream=True, timeout=120) as r:
                if r.status_code == 401:
                    raise SystemExit("❌ Unauthorized. Check your API key.")
                
                if r.status_code == 503:
                    print(f"⏳ Engine booting (503). Waiting 15s... (Attempt {attempts+1})")
                    time.sleep(15)
                    attempts += 1
                    continue
                    
                r.raise_for_status()
                
                for line in r.iter_lines():
                    if line:
                        line = line.decode('utf-8').replace('data: ', '')
                        if line == '[DONE]': break
                        try:
                            chunk = json.loads(line)
                            content = chunk['choices'][0]['delta'].get('content', '')
                            full_text += content
                        except: pass
                
                return extract_html(full_text)
                
        except Exception as e:
            print(f"❌ Stream error: {e}")
            attempts += 1
            time.sleep(5)
            
    return None

def run_mutator(clean_path):
    seed = str(random.randint(1, 999999))
    result = subprocess.run(["node", "mutate.js", clean_path, seed], capture_output=True, text=True)
    if not result.stdout.strip(): return None, "Node execution failed"
    try:
        data = json.loads(result.stdout)
        if data.get("ok"): return data["html"], data["description"]
        return None, data.get("error", "Unknown mutation error")
    except Exception as e:
        return None, f"Failed to parse mutator output: {e}"

def run_evaluator(clean_path, test_path):
    result = subprocess.run(["node", "evaluate_code.js", clean_path, test_path], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": "Failed to parse evaluator output"}

def process_file(filepath):
    print(f"\n==========================================")
    print(f"📄 Processing: {filepath}")
    print(f"==========================================")
    
    with open(filepath, "r", encoding="utf-8") as f:
        clean_html = f.read()

    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_clean:
        f_clean.write(clean_html)
        clean_path = f_clean.name

    broken_html, desc = run_mutator(clean_path)
    if not broken_html:
        print(f"⚠️ {filepath} skipped: {desc}")
        os.unlink(clean_path)
        return
        
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_broken:
        f_broken.write(broken_html)
        broken_path = f_broken.name

    print(f"🧬 {filepath} Mutation: {desc}")
    eval_broken = run_evaluator(clean_path, broken_path)
    
    if eval_broken.get("is_fixed") or not eval_broken.get("errors"):
        print(f"⚠️ {filepath} skipped: No detectable crash.")
        os.unlink(clean_path)
        os.unlink(broken_path)
        return

    err_msg = eval_broken["errors"][0]
    print(f"🔴 {filepath} Broken. Error: {err_msg}")

    prompt = f"Fix this code. Error: {err_msg}\n\n```html\n{broken_html}\n```"
    num_samples = 2
    results = []
    
    for i in range(num_samples):
        fixed_html = stream_request(prompt)
        if not fixed_html: continue
            
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_fixed:
            f_fixed.write(fixed_html)
            fixed_path = f_fixed.name
            
        eval_res = run_evaluator(clean_path, fixed_path)
        results.append({"code": fixed_html, "passed": eval_res.get("is_fixed", False)})
        os.unlink(fixed_path)

    passes = [r["code"] for r in results if r["passed"]]
    fails = [r["code"] for r in results if not r["passed"]]

    if passes or fails:
        with file_lock:
            with open(OUTPUT_FILE, "a") as f:
                if passes and fails:
                    dpo_pair = {
                        "prompt": [{"role": "user", "content": prompt}],
                        "chosen": [{"role": "assistant", "content": passes[0]}],
                        "rejected": [{"role": "assistant", "content": fails[0]}]
                    }
                    f.write(json.dumps(dpo_pair) + "\n")
                    print(f"✅ {filepath} Saved DPO pair!")
                elif passes and not fails:
                    sft_pair = {
                        "messages": [
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": passes[0]}
                        ]
                    }
                    f.write(json.dumps(sft_pair) + "\n")
                    print(f"✅ {filepath} Saved SFT pair!")

    os.unlink(clean_path)
    os.unlink(broken_path)

def main():
    if not os.path.exists(INPUT_DIR):
        print(f"📁 No {INPUT_DIR} folder found.")
        return

    files = [os.path.join(INPUT_DIR, f) for f in os.listdir(INPUT_DIR) if f.endswith(".html")]
    if not files:
        print(f"⚠️ No .html files found in {INPUT_DIR}.")
        return

    print(f"🚀 Found {len(files)} HTML files. Booting up 3 parallel workers...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(process_file, file) for file in files]
        for future in concurrent.futures.as_completed(futures):
            try: future.result()
            except Exception as e: print(f"❌ Thread crashed: {e}")

if __name__ == "__main__":
    main()
import json, os, subprocess, random, tempfile, hashlib, requests, re, time

API_KEY = os.environ.get("SKRITIMAI_KEY")
SKRITIMAI_URL = os.environ.get("SKRITIMAI_URL", "https://skritimai.varakala-saisurya.workers.dev/v1/chat/completions")
if not API_KEY:
    raise SystemExit("❌ Set SKRITIMAI_KEY environment variable")

OUTPUT_FILE = "dpo_training_data.jsonl"
INPUT_DIR = "clean_html" # <--- Create this folder and put your HTML files here!

def extract_html(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    start = text.find('<!DOCTYPE')
    if start == -1: start = text.find('<html')
    end = text.rfind('</html>')
    if start != -1 and end != -1:
        return text[start:end+7]
    return text

def stream_request(prompt):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "skritimai",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.7
    }
    
    full_text = ""
    try:
        with requests.post(SKRITIMAI_URL, headers=headers, json=payload, stream=True, timeout=120) as r:
            if r.status_code == 401:
                raise SystemExit("❌ Unauthorized. Check your API key.")
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
    except Exception as e:
        print(f"❌ Stream error: {e}")
        return None
    return extract_html(full_text)

def run_mutator(clean_path):
    seed = str(random.randint(1, 999999))
    result = subprocess.run(["node", "mutate.js", clean_path, seed], capture_output=True, text=True)
    
    if not result.stdout.strip():
        return None, "Node execution failed"
        
    try:
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data["html"], data["description"]
        return None, data.get("error", "Unknown mutation error")
    except Exception as e:
        return None, f"Failed to parse mutator output: {e}"

def run_evaluator(clean_path, test_path):
    result = subprocess.run(["node", "evaluate_code.js", clean_path, test_path], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": "Failed to parse evaluator output", "raw": result.stdout}

def process_file(filepath):
    print(f"\n==========================================")
    print(f"📄 Processing: {filepath}")
    print(f"==========================================")
    
    with open(filepath, "r", encoding="utf-8") as f:
        clean_html = f.read()

    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_clean:
        f_clean.write(clean_html)
        clean_path = f_clean.name

    # 1. Mutate
    broken_html, desc = run_mutator(clean_path)
    if not broken_html:
        print(f"⚠️ Skipping: Mutation failed ({desc})")
        os.unlink(clean_path)
        return
        
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_broken:
        f_broken.write(broken_html)
        broken_path = f_broken.name

    print(f"🧬 Mutation: {desc}")

    # 2. Verify it broke
    eval_broken = run_evaluator(clean_path, broken_path)
    if eval_broken.get("is_fixed") or not eval_broken.get("errors"):
        print(f"⚠️ Skipping: Mutation didn't cause a detectable error.")
        os.unlink(clean_path)
        os.unlink(broken_path)
        return

    err_msg = eval_broken["errors"][0]
    print(f"🔴 Broken successfully. Error: {err_msg}")

    # 3. Ask AI for fixes
    prompt = f"Fix this code. Error: {err_msg}\n\n```html\n{broken_html}\n```"
    num_samples = 2 # Reduced to 2 to save time during bulk generation
    results = []
    
    for i in range(num_samples):
        print(f"--- Sampling fix {i+1}/{num_samples} ---")
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

    # 4. Save Data
    if passes and fails:
        dpo_pair = {
            "prompt": [{"role": "user", "content": prompt}],
            "chosen": [{"role": "assistant", "content": passes[0]}],
            "rejected": [{"role": "assistant", "content": fails[0]}]
        }
        with open(OUTPUT_FILE, "a") as f:
            f.write(json.dumps(dpo_pair) + "\n")
        print(f"✅ Saved DPO pair!")
        
    elif passes and not fails:
        sft_pair = {
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": passes[0]}
            ]
        }
        with open(OUTPUT_FILE, "a") as f:
            f.write(json.dumps(sft_pair) + "\n")
        print(f"✅ Saved SFT pair!")
        
    else:
        print(f"⚠️ No valid pairs (Passes: {len(passes)}, Fails: {len(fails)})")

    os.unlink(clean_path)
    os.unlink(broken_path)

def main():
    # Create directory if it doesn't exist
    if not os.path.exists(INPUT_DIR):
        os.makedirs(INPUT_DIR)
        print(f"📁 Created {INPUT_DIR} folder. Please add .html files to it and run again.")
        return

    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".html")]
    if not files:
        print(f"⚠️ No .html files found in {INPUT_DIR}. Please add some to generate data.")
        return

    print(f"🚀 Found {len(files)} HTML files to process.")
    
    for i, filename in enumerate(files):
        filepath = os.path.join(INPUT_DIR, filename)
        process_file(filepath)
        
        # Sleep to prevent rate-limiting on the Cloudflare Worker / Kaggle
        if i < len(files) - 1:
            print("Waiting 5 seconds before next file...")
            time.sleep(5)

if __name__ == "__main__":
    main()
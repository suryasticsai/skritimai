import json, os, subprocess, random, tempfile, hashlib, requests, re

API_KEY = os.environ.get("SKRITIMAI_KEY")
SKRITIMAI_URL = os.environ.get("SKRITIMAI_URL", "https://skritimai.varakala-saisurya.workers.dev/v1/chat/completions")
if not API_KEY:
    raise SystemExit("❌ Set SKRITIMAI_KEY environment variable")

OUTPUT_FILE = "dpo_training_data.jsonl"

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
    # Calls your new AST-based mutator
    result = subprocess.run(["node", "mutate.js", clean_path], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data["html"], data["description"]
        return None, data.get("error", "Unknown mutation error")
    except Exception as e:
        return None, f"Failed to parse mutator output: {e}"

def run_evaluator(clean_path, test_path):
    # Calls your new robust evaluator
    result = subprocess.run(["node", "evaluate_code.js", clean_path, test_path], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": "Failed to parse evaluator output", "raw": result.stdout}

def main():
    # A simple clean HTML to test
    clean_html = """
    <!DOCTYPE html>
    <html>
    <body>
        <h1 id="title">Hello</h1>
        <button id="btn">Click me</button>
        <script>
            document.getElementById("btn").addEventListener("click", function() {
                let x = 5;
                if (x === 5) {
                    document.getElementById("title").innerText = "World";
                }
            });
        </script>
    </body>
    </html>
    """
    
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_clean:
        f_clean.write(clean_html)
        clean_path = f_clean.name

    # 1. Mutate using the new AST mutator
    broken_html, desc = run_mutator(clean_path)
    if not broken_html:
        print(f"Mutation failed: {desc}. Exiting.")
        os.unlink(clean_path)
        return
        
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_broken:
        f_broken.write(broken_html)
        broken_path = f_broken.name

    print(f"🧬 Mutation: {desc}")

    # 2. Verify it broke
    eval_broken = run_evaluator(clean_path, broken_path)
    if eval_broken.get("is_fixed") or not eval_broken.get("errors"):
        print(f"Mutation didn't cause a detectable error. Skipping.")
        os.unlink(clean_path)
        os.unlink(broken_path)
        return

    err_msg = eval_broken["errors"][0]
    print(f"🔴 Broken successfully. Error: {err_msg}")

    prompt = f"Fix this code. Error: {err_msg}\n\n```html\n{broken_html}\n```"
    num_samples = 3
    results = []
    
    for i in range(num_samples):
        print(f"\n--- Sampling fix {i+1}/{num_samples} ---")
        fixed_html = stream_request(prompt)
        
        if not fixed_html:
            print("❌ AI returned empty string.")
            continue
            
        print(f"🤖 AI Output Length: {len(fixed_html)} chars")
        
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_fixed:
            f_fixed.write(fixed_html)
            fixed_path = f_fixed.name
            
        eval_res = run_evaluator(clean_path, fixed_path)
        
        print(f"🧪 Eval Result: {eval_res}")
        
        results.append({"code": fixed_html, "passed": eval_res.get("is_fixed", False)})
        os.unlink(fixed_path)

    passes = [r["code"] for r in results if r["passed"]]
    fails = [r["code"] for r in results if not r["passed"]]

    # Save as DPO if we have both, or SFT if it passed 100%
    if passes and fails:
        dpo_pair = {
            "prompt": [{"role": "user", "content": prompt}],
            "chosen": [{"role": "assistant", "content": passes[0]}],
            "rejected": [{"role": "assistant", "content": fails[0]}]
        }
        pair_hash = hashlib.md5(json.dumps(dpo_pair).encode()).hexdigest()
        with open(OUTPUT_FILE, "a") as f:
            f.write(json.dumps(dpo_pair) + "\n")
        print(f"\n✅ Saved 1 DPO pair! (Hash: {pair_hash})")
        
    elif passes and not fails:
        sft_pair = {
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": passes[0]}
            ]
        }
        pair_hash = hashlib.md5(json.dumps(sft_pair).encode()).hexdigest()
        with open(OUTPUT_FILE, "a") as f:
            f.write(json.dumps(sft_pair) + "\n")
        print(f"\n✅ AI passed 3/3 times. Saved 1 SFT pair! (Hash: {pair_hash})")
        
    else:
        print(f"\n⚠️ No valid pairs (Passes: {len(passes)}, Fails: {len(fails)})")

    os.unlink(clean_path)
    os.unlink(broken_path)

if __name__ == "__main__":
    main()
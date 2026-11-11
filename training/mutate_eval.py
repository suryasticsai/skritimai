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

def mutate_script(html):
    script_regex = r'(<script\b[^>]*>)([\s\S]*?)(</script>)'
    scripts = re.findall(script_regex, html, re.IGNORECASE)
    if not scripts:
        return html, "No script found"
    
    original_script = scripts[0][1]
    mutations = [
        (r'===', r'!==', "Flipped === to !=="),
        (r'\b(let|const|var)\s+(\w+)', r'\1 \2_typo', "Renamed variable"),
        (r'\+', r'-', "Changed + to -"),
    ]
    
    random.shuffle(mutations)
    for pattern, replacement, desc in mutations:
        mutated_js = re.sub(pattern, replacement, original_script, count=1)
        if mutated_js != original_script:
            mutated_html = html.replace(original_script, mutated_js)
            return mutated_html, desc
    return html, "Mutation failed"

def run_evaluator(clean_path, test_path):
    cmd = ["node", "evaluate_code.js", clean_path, test_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": "Failed to parse node output", "raw": result.stdout, "stderr": result.stderr}

def main():
    # Valid HTML with a script tag
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

    broken_html, desc = mutate_script(clean_html)
    if desc == "No script found":
        print(f"Mutation: {desc}. Exiting.")
        return
        
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f_broken:
        f_broken.write(broken_html)
        broken_path = f_broken.name

    print(f"Mutation: {desc}")

    eval_broken = run_evaluator(clean_path, broken_path)
    if eval_broken.get("is_fixed") or not eval_broken.get("errors"):
        print(f"Mutation didn't cause a detectable error. Skipping.")
        return

    err_msg = eval_broken["errors"][0]
    print(f"Broken successfully. Error: {err_msg}")

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
    else:
        print(f"\n⚠️ No valid DPO pair (Passes: {len(passes)}, Fails: {len(fails)})")

    os.unlink(clean_path)
    os.unlink(broken_path)

if __name__ == "__main__":
    main()
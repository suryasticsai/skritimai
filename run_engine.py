import os, re, sys, time, json, shutil, subprocess, textwrap
import urllib.request, urllib.error
from pathlib import Path

# ---- Get inputs from GitHub Actions via command line arguments ----
if len(sys.argv) < 8:
    print("Error: Missing arguments. Did you update your YAML file?")
    sys.exit(1)

MODEL       = sys.argv[1]
FALLBACK    = sys.argv[2]
EXTRA       = sys.argv[3]
NUM_CTX     = int(sys.argv[4])
DRAFT       = int(sys.argv[5])
RUNTIME_MIN = int(sys.argv[6])
API_KEY     = sys.argv[7]

# Optional Gist secrets (if empty, the script just skips them)
GIST_ID     = ""
GIST_TOKEN  = ""

ENGINE_NAME   = "skritimai"          # the model name your clients use
SYSTEM_PROMPT = ("You are Skritimai, a private, self-hosted AI engine. "
                 "Be accurate, concise and practical. Say so when you are unsure.")

WORK   = "/kaggle/working"
MODELS = Path(WORK) / "ollama-models"
OLLAMA = "http://127.0.0.1:11434"
MODELS.mkdir(parents=True, exist_ok=True)

if not API_KEY:
    raise SystemExit("ENGINE_API_KEY secret is missing - refusing to expose an open endpoint.")

extra_models = [m.strip() for m in EXTRA.split(",") if m.strip()]
for name in [MODEL, FALLBACK] + extra_models:
    if not re.fullmatch(r"[A-Za-z0-9._:/@-]+", name):
        raise SystemExit(f"Refusing suspicious model name: {name!r}")

# ---------- helpers ----------
def sh(cmd, check=True, env=None):
    print("$", cmd, flush=True)
    r = subprocess.run(cmd, shell=True, env=env)
    if check and r.returncode:
        raise SystemExit(f"command failed ({r.returncode}): {cmd}")
    return r.returncode

def bg(cmd, log, env=None):
    print("$ (bg)", cmd, flush=True)
    e = dict(os.environ, **(env or {}))
    return subprocess.Popen(cmd, shell=True, stdout=open(f"{WORK}/{log}", "w"),
                            stderr=subprocess.STDOUT, env=e)

def wait_http(url, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=5).close()
            return True
        except Exception:
            time.sleep(2)
    return False

def post_json(url, payload, timeout=900, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def publish(state):
    """Write the engine's current public URL to a private gist so clients can find it."""
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not (GIST_ID and GIST_TOKEN):
        print("STATE:", json.dumps(state)); return
    body = json.dumps({"files": {"engine.json": {"content": json.dumps(state, indent=2)}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{GIST_ID}", data=body, method="PATCH",
        headers={"Authorization": f"Bearer {GIST_TOKEN}",
                 "Accept": "application/vnd.github+json", "User-Agent": "ai-engine"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except Exception as e:
        print("gist publish failed:", e)

# ---------- 1. GPU check + model choice ----------
sh("nvidia-smi", check=False)
try:
    out = subprocess.check_output(
        "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits", shell=True, text=True)
    gpus = [int(x) for x in out.split()]
except Exception:
    gpus = []
total_mb = sum(gpus)
print(f"GPUs: {len(gpus)}  VRAM per GPU (MiB): {gpus}  total: {total_mb}")

if total_mb >= 28000:
    primary, ctx, parallel = MODEL, NUM_CTX, "1"
else:
    primary, ctx, parallel = FALLBACK, min(NUM_CTX, 32768), "2"
    print(f"!! Only {total_mb} MiB VRAM - the 27B model will not fit. Falling back to {FALLBACK}.")

# ---------- 2. Install + start Ollama ----------
sh("apt-get update -qq && apt-get install -y -qq zstd curl", check=False)
sh("pip install -q fastapi uvicorn httpx")
sh("curl -fsSL https://ollama.com/install.sh | sh")
OLLAMA_BIN = shutil.which("ollama")
if not OLLAMA_BIN:
    raise SystemExit("ollama binary not found after install")

ENV = dict(os.environ)
ENV.update({
    "OLLAMA_HOST": "127.0.0.1:11434",
    "OLLAMA_MODELS": str(MODELS),
    "OLLAMA_KEEP_ALIVE": "-1",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
    "OLLAMA_CONTEXT_LENGTH": str(ctx),
    "OLLAMA_NUM_PARALLEL": parallel,
    "OLLAMA_MAX_LOADED_MODELS": "2",   # chat model + small embedding model
})
ollama_proc = subprocess.Popen([OLLAMA_BIN, "serve"], env=ENV,
                               stdout=open(f"{WORK}/ollama.log", "w"), stderr=subprocess.STDOUT)
if not wait_http(f"{OLLAMA}/api/tags", 90):
    sh(f"tail -n 50 {WORK}/ollama.log", check=False)
    raise SystemExit("Ollama did not start")

# ---------- 3. Pull models ----------
def pull(name):
    if subprocess.run([OLLAMA_BIN, "pull", name], env=ENV).returncode == 0:
        return
    print("standard pull failed; retrying with --insecure")
    subprocess.run([OLLAMA_BIN, "pull", "--insecure", name], env=ENV, check=True)

for name in [primary] + extra_models:
    pull(name)

# ---------- 4. Create the branded engine model ----------
create = {"model": ENGINE_NAME, "from": primary, "system": SYSTEM_PROMPT,
          "parameters": {"num_ctx": ctx, "draft_num_predict": DRAFT}, "stream": False}
try:
    print(post_json(f"{OLLAMA}/api/create", create, timeout=300))
except urllib.error.HTTPError as e:
    print("create with draft_num_predict failed:", e.read()[:300], "- retrying without it")
    create["parameters"].pop("draft_num_predict")
    print(post_json(f"{OLLAMA}/api/create", create, timeout=300))

# load + pin it so the first real request is fast
r = post_json(f"{OLLAMA}/api/generate",
              {"model": ENGINE_NAME, "prompt": "Reply with exactly: Engine is working.",
               "stream": False, "keep_alive": -1, "think": False})
print("Warm-up:", r.get("response", "").strip())
sh(f"{OLLAMA_BIN} ps", check=False, env=ENV)
sh("nvidia-smi", check=False)

# ---------- 5. Auth + allow-list gateway in front of Ollama ----------
PROXY = textwrap.dedent('''
    import os, secrets, httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.background import BackgroundTask

    KEY = os.environ["ENGINE_API_KEY"]
    ALLOWED = ("v1/", "api/chat", "api/generate", "api/tags", "api/embed",
               "api/embeddings", "api/show", "api/ps")
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])
    client = httpx.AsyncClient(base_url="http://127.0.0.1:11434", timeout=None)

    @app.get("/health")
    async def health():
        try:
            r = await client.get("/api/tags", timeout=5)
            return {"status": "ok", "models": [m["name"] for m in r.json().get("models", [])]}
        except Exception:
            return JSONResponse({"status": "down"}, status_code=503)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {KEY}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not path.startswith(ALLOWED):
            return JSONResponse({"error": "endpoint not allowed"}, status_code=403)
        body = await request.body()
        req = client.build_request(
            request.method, "/" + path, content=body,
            headers={"content-type": request.headers.get("content-type", "application/json")},
            params=request.query_params)
        r = await client.send(req, stream=True)
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type"),
                                 background=BackgroundTask(r.aclose))
''')
Path(f"{WORK}/proxy.py").write_text(PROXY)
proxy_proc = bg(f"cd {WORK} && python -m uvicorn proxy:app --host 127.0.0.1 --port 8000",
                "proxy.log", env={"ENGINE_API_KEY": API_KEY})
if not wait_http("http://127.0.0.1:8000/health", 60):
    sh(f"tail -n 50 {WORK}/proxy.log", check=False)
    raise SystemExit("Gateway did not start")

# ---------- 6. Public tunnel -> gateway ----------
sh("wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared && chmod +x cloudflared")
tunnel_proc = bg("./cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 "
                 "--url http://127.0.0.1:8000", "cloudflared.log")
url = None
for _ in range(45):
    time.sleep(2)
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", Path(f"{WORK}/cloudflared.log").read_text())
    if m:
        url = m.group(0); break
if not url:
    sh(f"cat {WORK}/cloudflared.log", check=False)
    raise SystemExit("No tunnel URL")

# end-to-end self-test through the public URL
ok = False
for _ in range(10):
    try:
        r = post_json(url + "/api/chat",
                      {"model": ENGINE_NAME, "stream": False, "think": False,
                       "messages": [{"role": "user", "content": "Say hello in five words."}]},
                      timeout=300, headers={"Authorization": f"Bearer {API_KEY}"})
        print("Self-test reply:", r["message"]["content"].strip())
        ok = True
        break
    except Exception as e:
        print("self-test retry:", e)
        time.sleep(6)

state = {"status": "online" if ok else "degraded", "url": url, "openai_base_url": url + "/v1",
         "engine": ENGINE_NAME, "base_model": primary, "num_ctx": ctx,
         "gpus_mib": gpus, "models": [ENGINE_NAME] + extra_models}
publish(state)
print("=" * 60)
print("ENGINE", "ONLINE" if ok else "UP BUT SELF-TEST FAILED", url)
print("OpenAI-compatible base URL:", url + "/v1")
print("Model name:", ENGINE_NAME)
print("=" * 60, flush=True)

# ---------- 7. Supervise until the deadline ----------
deadline = time.time() + RUNTIME_MIN * 60
last_pub = time.time()
while time.time() < deadline:
    time.sleep(30)
    dead = [n for n, p in (("ollama", ollama_proc), ("gateway", proxy_proc), ("tunnel", tunnel_proc))
            if p.poll() is not None]
    if dead:
        print("process died:", dead)
        for log in ("ollama.log", "proxy.log", "cloudflared.log"):
            sh(f"tail -n 20 {WORK}/{log}", check=False)
        break
    if time.time() - last_pub > 300:
        publish(state); last_pub = time.time()

publish({"status": "offline", "url": None, "engine": ENGINE_NAME})
print("Engine shut down.")
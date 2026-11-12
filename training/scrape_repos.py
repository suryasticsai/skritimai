import os
import requests
import time

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    print("❌ GITHUB_TOKEN missing. Skipping scrape.")
    exit(0)

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}
OUTPUT_DIR = "clean_html"
MAX_FILES = 5  # Change this to 50 to go faster
os.makedirs(OUTPUT_DIR, exist_ok=True)

QUERIES = [
    "vanilla+javascript+game+in:readme",
    "html+css+javascript+calculator+in:readme",
    "vanilla+js+dashboard+in:readme"
]

def download_file(repo_name, file_path, download_url, file_name):
    try:
        res = requests.get(download_url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            content = res.text
            if "<script>" in content.lower() and "<html" in content.lower():
                filepath = os.path.join(OUTPUT_DIR, file_name)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"✅ Downloaded: {file_name} from {repo_name}")
                return True
    except Exception as e:
        print(f"⚠️ Failed to download {file_name}: {e}")
    return False

def main():
    files_downloaded = 0
    for query in QUERIES:
        if files_downloaded >= MAX_FILES:
            break
            
        print(f"🔍 Searching GitHub for: {query}")
        url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=5"
        res = requests.get(url, headers=HEADERS, timeout=10)
        
        if res.status_code != 200:
            print(f"❌ GitHub API Error: {res.status_code}")
            continue
            
        repos = res.json().get("items", [])
        for repo in repos:
            if files_downloaded >= MAX_FILES:
                break
                
            repo_name = repo["full_name"]
            default_branch = repo["default_branch"]
            
            tree_url = f"https://api.github.com/repos/{repo_name}/git/trees/{default_branch}?recursive=1"
            tree_res = requests.get(tree_url, headers=HEADERS, timeout=10)
            if tree_res.status_code != 200:
                continue
                
            tree = tree_res.json().get("tree", [])
            html_files = [item for item in tree if item["path"].endswith(".html")]
            
            for html_file in html_files:
                if files_downloaded >= MAX_FILES:
                    break
                
                safe_name = f"{repo_name.replace('/', '_')}_{html_file['path'].replace('/', '_')}"
                raw_url = f"https://raw.githubusercontent.com/{repo_name}/{default_branch}/{html_file['path']}"
                
                if download_file(repo_name, html_file['path'], raw_url, safe_name):
                    files_downloaded += 1
                
                time.sleep(1)

if __name__ == "__main__":
    main()
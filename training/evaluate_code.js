const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("error", (msg) => errors.push(String(msg)));
virtualConsole.on("jsdomError", (e) => errors.push(e.message));

const beforeParse = (window) => {
    window.HTMLCanvasElement.prototype.getContext = () => {};
    window.alert = () => {};
    window.confirm = () => true;
    window.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {} });
    window.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
    window.addEventListener("error", (e) => errors.push(e.message));
    window.addEventListener("unhandledrejection", (e) => errors.push(e.reason));
};

async function runInteraction(html) {
    errors.length = 0; 
    let finalDom = "";
    
    try {
        const dom = new JSDOM(html, {
            runScripts: "dangerously",
            virtualConsole,
            beforeParse
        });

        const { window } = dom;
        
        await new Promise((resolve) => {
            window.addEventListener("load", () => {
                const buttons = window.document.querySelectorAll("button, input[type='button']");
                buttons.forEach(btn => {
                    try { btn.click(); } catch(e) { errors.push(e.message); }
                });

                const inputs = window.document.querySelectorAll("input[type='text'], textarea");
                inputs.forEach(inp => {
                    inp.value = "test";
                    inp.dispatchEvent(new window.Event('input', { bubbles: true }));
                    inp.dispatchEvent(new window.Event('change', { bubbles: true }));
                });

                setTimeout(resolve, 300);
            });
        });

        finalDom = window.document.body.innerHTML;
    } catch (e) {
        errors.push(e.message);
    }

    return { success: errors.length === 0, errors, dom: finalDom };
}

// CRITICAL: Normalize whitespace so AI formatting changes don't fail the test
const normalizeDom = (html) => html.replace(/\s+/g, ' ').trim();

async function main() {
    const cleanFile = process.argv[2];
    const testFile = process.argv[3];

    const cleanHtml = fs.readFileSync(cleanFile, "utf-8");
    const testHtml = fs.readFileSync(testFile, "utf-8");

    const cleanResult = await runInteraction(cleanHtml);
    if (!cleanResult.success) {
        console.log(JSON.stringify({ valid_clean: false, reason: "Clean file failed", errors: cleanResult.errors }));
        return;
    }

    const testResult = await runInteraction(testHtml);
    const domMatch = normalizeDom(cleanResult.dom) === normalizeDom(testResult.dom);

    console.log(JSON.stringify({
        valid_clean: true,
        success: testResult.success,
        dom_match: domMatch,
        errors: testResult.errors,
        is_fixed: testResult.success && domMatch 
    }));
}

main();
const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");

// 1. Setup Virtual Console to catch everything
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("error", (msg) => errors.push(String(msg)));
virtualConsole.on("jsdomError", (e) => errors.push(e.message));

// 2. Stub missing APIs (Point 3)
const beforeParse = (window) => {
    window.HTMLCanvasElement.prototype.getContext = () => {};
    window.alert = () => {};
    window.confirm = () => true;
    window.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {} });
    window.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
    window.addEventListener("error", (e) => errors.push(e.message));
    window.addEventListener("unhandledrejection", (e) => errors.push(e.reason));
};

async function runInteraction(html, isClean) {
    errors.length = 0; // Reset errors
    let finalDom = "";
    
    try {
        const dom = new JSDOM(html, {
            runScripts: "dangerously", // Point 10: Run in isolated CI runner, not locally
            resources: "usable",       // Point 10: Consider removing this for safety
            virtualConsole,
            beforeParse
        });

        const { window } = dom;
        
        // Point 2: Wait for full load, then simulate interactions
        await new Promise((resolve) => {
            window.addEventListener("load", () => {
                // Click every button
                const buttons = window.document.querySelectorAll("button, input[type='button']");
                buttons.forEach(btn => {
                    try { btn.click(); } catch(e) { errors.push(e.message); }
                });

                // Dispatch input events
                const inputs = window.document.querySelectorAll("input[type='text'], textarea");
                inputs.forEach(inp => {
                    inp.value = "test";
                    inp.dispatchEvent(new window.Event('input', { bubbles: true }));
                    inp.dispatchEvent(new window.Event('change', { bubbles: true }));
                });

                // Wait a bit for async DOM updates
                setTimeout(resolve, 500);
            });
        });

        finalDom = window.document.body.innerHTML;
        
    } catch (e) {
        errors.push(e.message);
    }

    return { success: errors.length === 0, errors, dom: finalDom };
}

async function main() {
    const cleanFile = process.argv[2];
    const testFile = process.argv[3];

    const cleanHtml = fs.readFileSync(cleanFile, "utf-8");
    const testHtml = fs.readFileSync(testFile, "utf-8");

    // Establish the ground truth
    const cleanResult = await runInteraction(cleanHtml, true);
    if (!cleanResult.success) {
        console.log(JSON.stringify({ valid_clean: false, reason: "Clean file failed", errors: cleanResult.errors }));
        return;
    }

    // Test the mutated/fixed file
    const testResult = await runInteraction(testHtml, false);
    
    // Point 5: The DOM comparison oracle (prevents gaming by deleting script)
    const domMatch = cleanResult.dom === testResult.dom;

    console.log(JSON.stringify({
        valid_clean: true,
        success: testResult.success,
        dom_match: domMatch,
        errors: testResult.errors,
        is_fixed: testResult.success && domMatch // Must run AND match clean DOM
    }));
}

main();
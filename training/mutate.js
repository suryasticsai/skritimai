const fs = require('fs');
const acorn = require('acorn');

const args = process.argv.slice(2);
const filePath = args[0];
const seed = parseInt(args[1] || "1", 10);

if (!filePath) {
    console.log(JSON.stringify({ ok: false, error: "No file provided" }));
    process.exit(0);
}

const html = fs.readFileSync(filePath, 'utf-8');

let s = seed;
const rng = () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
};

const scriptRegex = /(<script\b[^>]*>)([\s\S]*?)(<\/script>)/gi;
const scripts = [...html.matchAll(scriptRegex)];

if (scripts.length === 0) {
    console.log(JSON.stringify({ ok: false, error: "No <script> tags found" }));
    process.exit(0);
}

const script = scripts[0];
const scriptContent = script[2];
const startOffset = script.index + script[1].length;

let ast;
try {
    ast = acorn.parse(scriptContent, { ecmaVersion: "latest", locations: true });
} catch (e) {
    console.log(JSON.stringify({ ok: false, error: "Syntax error in JS: " + e.message }));
    process.exit(0);
}

const candidates = [];
const SWAP = { "===": "!==", "!==": "===", "==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "+": "-", "-": "+", "*": "/", "/": "*", "&&": "||", "||": "&&", "%": "*" };

function walk(node, parent) {
    if (!node || typeof node.type !== 'string') return;
    
    if (node.type === "BinaryExpression" || node.type === "LogicalExpression") {
        if (SWAP[node.operator]) {
            candidates.push({
                type: "operator",
                start: startOffset + node.left.end,
                end: startOffset + node.right.start,
                replacement: ` ${SWAP[node.operator]} `,
                desc: `Flipped ${node.operator} to ${SWAP[node.operator]}`,
                line: node.loc.start.line
            });
        }
    }
    
    if (node.type === "Identifier" && parent) {
        if (parent.type !== "MemberExpression" || parent.property !== node) {
             if (rng() > 0.5) {
                 candidates.push({
                    type: "var",
                    start: startOffset + node.start,
                    end: startOffset + node.end,
                    replacement: node.name + "_typo",
                    desc: `Renamed variable ${node.name}`,
                    line: node.loc.start.line
                });
             }
        }
    }

    for (const key in node) {
        if (key === 'loc' || key === 'start' || key === 'end') continue;
        const child = node[key];
        if (Array.isArray(child)) {
            child.forEach(c => walk(c, node));
        } else if (child && typeof child.type === 'string') {
            walk(child, node);
        }
    }
}

walk(ast, null);

if (candidates.length === 0) {
    console.log(JSON.stringify({ ok: false, error: "No suitable mutations found" }));
    process.exit(0);
}

const cand = candidates[Math.floor(rng() * candidates.length)];
const mutatedScriptContent = scriptContent.substring(0, cand.start - startOffset) + cand.replacement + scriptContent.substring(cand.end - startOffset);
const mutatedHtml = html.substring(0, script.index) + script[1] + mutatedScriptContent + script[3] + html.substring(script.index + script[0].length);

console.log(JSON.stringify({
    ok: true,
    html: mutatedHtml,
    kind: cand.type,
    description: cand.desc,
    line: cand.line
}));
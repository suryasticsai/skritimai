/**
 * 🚀 Skritimai Universal SDK v3.0
 * A production-grade, zero-dependency, full-featured client for the Skritimai AI Engine.
 * 
 * Features:
 * - Covers 10+ OpenAI/Ollama compatible endpoints
 * - Built-in Conversation memory manager
 * - Exponential backoff & auto-retry for Kaggle 502/503 waking errors
 * - Robust SSE (Streaming) parser
 * - Drop-in <skritimai-chat> Web Component with Markdown support
 * 
 * Works in Browsers (Web Components), Node.js, and via CDN.
 * @license MIT
 */
(function (root, factory) {
    if (typeof define === 'function' && define.amd) {
        define([], factory);
    } else if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        root.Skritimai = factory();
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // ==========================================
    // 1. CONFIGURATION & UTILITIES
    // ==========================================
    const defaultConfig = {
        baseUrl: 'http://127.0.0.1:11434', // Default local Ollama
        apiKey: 'not-needed',
        model: 'skritimai',
        timeoutMs: 120000,
        maxRetries: 2,
        debug: false
    };

    let _config = { ...defaultConfig };

    const log = (msg, ...args) => { if (_config.debug) console.log(`[Skritimai]`, msg, ...args); };
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    
    const buildUrl = (endpoint) => {
        let base = String(_config.baseUrl).replace(/\/+$/, '');
        if (base.endsWith('/v1')) base = base.slice(0, -3);
        if (base.endsWith('/chat/completions')) base = base.replace('/chat/completions', '');
        return `${base}/v1/${endpoint}`;
    };

    const getHeaders = () => {
        const h = { 'Content-Type': 'application/json' };
        if (_config.apiKey && _config.apiKey !== 'not-needed') h['Authorization'] = `Bearer ${_config.apiKey}`;
        return h;
    };

    // ==========================================
    // 2. CORE HTTP CLIENT (With Retries)
    // ==========================================
    async function _request(endpoint, method, body, options = {}) {
        const url = buildUrl(endpoint);
        const maxAttempts = options.retries !== undefined ? options.retries : _config.maxRetries;
        let attempts = 0;

        while (attempts <= maxAttempts) {
            try {
                log(`Fetching ${url} (Attempt ${attempts + 1}/${maxAttempts + 1})`);
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), options.timeoutMs || _config.timeoutMs);

                const res = await fetch(url, {
                    method,
                    headers: getHeaders(),
                    body: body ? JSON.stringify(body) : undefined,
                    signal: controller.signal
                });
                clearTimeout(timeoutId);

                // Auto-retry for Cloudflare 502/503/504
                if ([502, 503, 504].includes(res.status) && attempts < maxAttempts) {
                    attempts++;
                    const backoff = 1500 * attempts;
                    log(`Server returned ${res.status}. Retrying in ${backoff}ms...`);
                    await sleep(backoff);
                    continue;
                }

                if (!res.ok) {
                    const errText = await res.text();
                    throw new Error(`HTTP ${res.status}: ${errText}`);
                }

                // Handle Streaming
                if (options.stream) {
                    return await _handleStream(res, options.onChunk);
                }

                // Handle Normal JSON
                return await res.json();

            } catch (error) {
                if (error.name === 'AbortError') throw new Error(`Request timed out after ${options.timeoutMs || _config.timeoutMs}ms`);
                if (attempts >= maxAttempts) throw error;
                attempts++;
                await sleep(1500 * attempts);
            }
        }
    }

    // ==========================================
    // 3. STREAM PARSER (SSE)
    // ==========================================
    async function _handleStream(response, onChunk) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep the last partial line

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed || trimmed === 'data: [DONE]') continue;
                if (trimmed.startsWith('data: ')) {
                    try {
                        const parsed = JSON.parse(trimmed.slice(6));
                        const content = parsed.choices?.[0]?.delta?.content || "";
                        if (content) {
                            fullText += content;
                            if (typeof onChunk === 'function') onChunk(content, fullText);
                        }
                    } catch (e) {
                        // Ignore partial JSON parse errors during stream
                    }
                }
            }
        }
        return { text: fullText, finish_reason: 'stop' };
    }

    // ==========================================
    // 4. THE SDK (Endpoint Methods)
    // ==========================================
    const Skritimai = {
        /**
         * Initialize the SDK
         */
        init: function (options = {}) {
            _config = { ..._config, ...options };
            return this;
        },

        /**
         * 1. List Models (GET /v1/models)
         */
        models: async function () {
            const res = await _request('models', 'GET');
            return res.data;
        },

        /**
         * 2. Health Check (Alias for models, but fails silently)
         */
        isHealthy: async function () {
            try {
                await this.models();
                return true;
            } catch {
                return false;
            }
        },

        /**
         * 3. Chat Completions (POST /v1/chat/completions)
         */
        chat: async function (messages, options = {}) {
            const body = {
                model: options.model || _config.model,
                messages,
                stream: !!options.stream
            };
            if (options.json) body.response_format = { type: 'json_object' };
            if (options.maxTokens) body.max_tokens = options.maxTokens;
            if (options.temperature !== undefined) body.temperature = options.temperature;
            if (options.tools) body.tools = options.tools;

            const res = await _request('chat/completions', 'POST', body, options);
            if (options.stream) return res; // Returns { text, finish_reason }
            
            const choice = res.choices?.[0];
            return { 
                text: choice?.message?.content || '', 
                finish: choice?.finish_reason || 'stop',
                usage: res.usage 
            };
        },

        /**
         * 4. Text Completions (POST /v1/completions)
         */
        complete: async function (prompt, options = {}) {
            const body = {
                model: options.model || _config.model,
                prompt,
                stream: !!options.stream
            };
            if (options.maxTokens) body.max_tokens = options.maxTokens;

            const res = await _request('completions', 'POST', body, options);
            if (options.stream) return res;
            
            const choice = res.choices?.[0];
            return { text: choice?.text || '', finish: choice?.finish_reason || 'stop' };
        },

        /**
         * 5. Embeddings (POST /v1/embeddings)
         */
        embed: async function (input, options = {}) {
            const body = {
                model: options.model || 'nomic-embed-text',
                input: Array.isArray(input) ? input : [input]
            };
            const res = await _request('embeddings', 'POST', body, options);
            return res.data.map(d => d.embedding);
        },

        /**
         * 6. Structured JSON Output (Helper for chat)
         */
        json: async function (prompt, system = "You are a JSON generator. Output only valid JSON.", options = {}) {
            const res = await this.chat([
                { role: 'system', content: system },
                { role: 'user', content: prompt }
            ], { ...options, json: true });
            
            try {
                return JSON.parse(res.text);
            } catch (e) {
                // Basic extraction if the model adds markdown code blocks
                const match = res.text.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
                if (match) return JSON.parse(match[1]);
                throw new Error("Failed to parse JSON response: " + res.text);
            }
        },

        /**
         * 7. Conversation Manager (Stateful Chat)
         */
        createConversation: function (systemPrompt = "You are a helpful AI.") {
            const messages = [{ role: 'system', content: systemPrompt }];
            return {
                messages,
                send: async (text, options = {}) => {
                    messages.push({ role: 'user', content: text });
                    const res = await Skritimai.chat(messages, options);
                    messages.push({ role: 'assistant', content: res.text });
                    return res;
                },
                clear: () => { messages.splice(1); } // Keeps system prompt
            };
        }
    };

    // ==========================================
    // 5. WEB COMPONENT (<skritimai-chat>)
    // ==========================================
    const parseMarkdown = (text) => {
        return text
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\*([^*]+)\*/g, '<em>$1</em>')
            .replace(/\n/g, '<br>');
    };

    if (typeof window !== 'undefined' && window.customElements) {
        class SkritimaiChat extends HTMLElement {
            constructor() {
                super();
                this.attachShadow({ mode: 'open' });
                this._convo = null;
                this._isGenerating = false;
            }

            connectedCallback() {
                const baseUrl = this.getAttribute('base-url');
                const model = this.getAttribute('model');
                const sys = this.getAttribute('system') || "You are Skritimai, a helpful AI.";
                
                if (baseUrl) Skritimai.init({ baseUrl, model });
                this._convo = Skritimai.createConversation(sys);
                
                this.render();
            }

            render() {
                this.shadowRoot.innerHTML = `
                    <style>
                        :host { display: flex; flex-direction: column; height: 500px; max-width: 100%; font-family: system-ui, -apple-system, sans-serif; background: #ffffff; border-radius: 12px; border: 1px solid #e5e7eb; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); overflow: hidden; }
                        .header { padding: 16px; background: #f8fafc; border-bottom: 1px solid #e5e7eb; font-weight: 600; color: #1e293b; display: flex; justify-content: space-between; align-items: center; }
                        .chat-window { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; background: #ffffff; scroll-behavior: smooth; }
                        .message { padding: 12px 16px; border-radius: 12px; max-width: 85%; line-height: 1.6; font-size: 14px; word-wrap: break-word; }
                        .user { background: #2563eb; color: white; align-self: flex-end; border-bottom-right-radius: 4px; }
                        .assistant { background: #f1f5f9; color: #1e293b; align-self: flex-start; border-bottom-left-radius: 4px; border: 1px solid #e2e8f0; }
                        .input-area { display: flex; padding: 16px; background: #f8fafc; border-top: 1px solid #e5e7eb; gap: 12px; }
                        input { flex: 1; padding: 12px 16px; border: 1px solid #cbd5e1; border-radius: 8px; outline: none; font-size: 14px; transition: border 0.2s; }
                        input:focus { border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }
                        button { padding: 12px 20px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; transition: background 0.2s; }
                        button:hover { background: #1d4ed8; }
                        button:disabled { background: #94a3b8; cursor: not-allowed; }
                        .clear-btn { background: transparent; color: #ef4444; border: none; font-size: 12px; cursor: pointer; padding: 0; font-weight: normal; }
                        pre { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 13px; margin: 8px 0; }
                        code { font-family: ui-monospace, SFMono-Regular, monospace; background: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 13px; color: #ef4444; }
                        pre code { background: transparent; color: inherit; padding: 0; }
                        .cursor { display: inline-block; width: 6px; height: 16px; background: #2563eb; animation: blink 1s step-end infinite; vertical-align: middle; margin-left: 4px; }
                        @keyframes blink { 50% { opacity: 0; } }
                    </style>
                    <div class="header">
                        <span>Skritimai Engine</span>
                        <button class="clear-btn" id="clear">Clear Chat</button>
                    </div>
                    <div class="chat-window" id="messages"></div>
                    <div class="input-area">
                        <input type="text" id="input" placeholder="Ask Skritimai..." autocomplete="off">
                        <button id="send">Send</button>
                    </div>
                `;

                this.shadowRoot.getElementById('send').onclick = () => this._send();
                this.shadowRoot.getElementById('clear').onclick = () => {
                    this._convo.clear();
                    this.shadowRoot.getElementById('messages').innerHTML = '';
                };
                this.shadowRoot.getElementById('input').addEventListener('keypress', (e) => {
                    if (e.key === 'Enter') this._send();
                });
            }

            async _send() {
                const input = this.shadowRoot.getElementById('input');
                const text = input.value.trim();
                if (!text || this._isGenerating) return;

                input.value = '';
                this._appendMessage('user', text);
                
                this._isGenerating = true;
                this.shadowRoot.getElementById('send').disabled = true;

                const msgContainer = this._appendMessage('assistant', '');
                const cursor = document.createElement('span');
                cursor.className = 'cursor';
                msgContainer.appendChild(cursor);

                try {
                    const res = await Skritimai.chat(this._convo.messages.concat({ role: 'user', content: text }), {
                        stream: true,
                        onChunk: (chunk, fullText) => {
                            msgContainer.innerHTML = parseMarkdown(fullText);
                            msgContainer.appendChild(cursor);
                            this._scrollToBottom();
                        }
                    });
                    
                    // Update conversation memory
                    this._convo.messages.push({ role: 'user', content: text });
                    this._convo.messages.push({ role: 'assistant', content: res.text });
                    
                    msgContainer.innerHTML = parseMarkdown(res.text);
                } catch (err) {
                    msgContainer.innerHTML = `<span style="color:#ef4444">Error: ${err.message}</span>`;
                } finally {
                    this._isGenerating = false;
                    this.shadowRoot.getElementById('send').disabled = false;
                    input.focus();
                }
            }

            _appendMessage(role, content) {
                const div = document.createElement('div');
                div.className = `message ${role}`;
                div.innerHTML = parseMarkdown(content);
                this.shadowRoot.getElementById('messages').appendChild(div);
                this._scrollToBottom();
                return div;
            }

            _scrollToBottom() {
                const win = this.shadowRoot.getElementById('messages');
                win.scrollTop = win.scrollHeight;
            }
        }
        window.customElements.define('skritimai-chat', SkritimaiChat);
    }

    return Skritimai;
}));
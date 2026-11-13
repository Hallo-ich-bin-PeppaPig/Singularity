import os
import json
import threading
import time
from collections import defaultdict, deque

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CF_MODEL = os.getenv("CF_MODEL", "@cf/qwen/qwen3.8-27b").strip()
RATE_LIMIT_RPM = max(1, min(int(os.getenv("RATE_LIMIT_RPM", "15")), 60))
MAX_TOKENS = max(128, min(int(os.getenv("MAX_TOKENS", "1200")), 4096))
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}" if CF_ACCOUNT_ID else ""

_hits = defaultdict(deque)
_hits_lock = threading.Lock()

def allowed_request():
    now = time.time()
    ip = request.remote_addr or "unknown"
    with _hits_lock:
        bucket = _hits[ip]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_RPM:
            return False
        bucket.append(now)
        if len(_hits) > 2000:
            for key in list(_hits)[:500]:
                if not _hits[key] or now - _hits[key][-1] >= 120:
                    _hits.pop(key, None)
        return True

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "public, max-age=300"
    return response

@app.get("/")
def home():
    return send_from_directory(app.root_path, "index.html")

@app.get("/health")
def health():
    # Liveness check: the web process is running even before provider credentials are added.
    configured = bool(CF_ACCOUNT_ID and CF_API_TOKEN)
    return jsonify({"status": "ok", "service": "singularity", "provider": "Cloudflare Workers AI", "model": CF_MODEL, "configured": configured}), 200

@app.get("/api/health")
def readiness():
    # Readiness check: reports whether chat requests can reach the configured provider.
    configured = bool(CF_ACCOUNT_ID and CF_API_TOKEN)
    return jsonify({"status": "ok" if configured else "configuration_required", "service": "singularity", "provider": "Cloudflare Workers AI", "model": CF_MODEL, "configured": configured}), 200 if configured else 503

@app.post("/api/chat")
def chat():
    if not allowed_request():
        return jsonify({"error": "Limite temporário de mensagens atingido. Aguarde um minuto e tente novamente."}), 429
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        return jsonify({"error": "O administrador ainda precisa configurar as credenciais do Cloudflare no ambiente do serviço."}), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Envie um objeto JSON válido."}), 400
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "A mensagem não pode ficar vazia."}), 400
    if len(message) > 12000:
        return jsonify({"error": "A mensagem excede o limite de 12.000 caracteres."}), 413
    mode = payload.get("mode", "Raciocínio")
    mode_hint = {
        "Raciocínio": "Responda com clareza e explique o raciocínio necessário, sem expor pensamentos privados internos.",
        "Pesquisa": "Diferencie fatos conhecidos de incertezas. Não afirme ter pesquisado na web se não usou uma ferramenta de busca.",
        "Código": "Priorize soluções técnicas corretas, exemplos seguros e código legível.",
    }.get(mode, "Responda de forma clara e útil.")
    messages = [{"role": "system", "content": "Você é a Singularity, uma assistente útil, honesta e cuidadosa. Responda no idioma do usuário. " + mode_hint}]
    history = payload.get("history", [])
    if isinstance(history, list):
        for item in history[-12:]:
            if not isinstance(item, dict):
                continue
            role, content = item.get("role"), item.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content[:12000]})
    messages.append({"role": "user", "content": message.strip()})
    wants_stream = payload.get("stream") is True
    body = {"messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0.6, "stream": wants_stream}
    try:
        upstream = requests.post(CF_URL, headers={"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}, json=body, timeout=(10, 105), stream=wants_stream)
        if upstream.status_code >= 400:
            app.logger.warning("Cloudflare Workers AI returned HTTP %s", upstream.status_code)
            if upstream.status_code in (401, 403):
                return jsonify({"error": "A Cloudflare recusou a credencial. Confira o token e as permissões Workers AI."}), 502
            if upstream.status_code == 429:
                return jsonify({"error": "O limite de uso do provedor foi atingido. Tente novamente mais tarde."}), 429
            return jsonify({"error": "O provedor de IA não conseguiu concluir a solicitação. Confira a configuração e tente novamente."}), 502
        if wants_stream:
            def generate():
                try:
                    for raw_line in upstream.iter_lines(decode_unicode=True):
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            event = json.loads(chunk)
                            delta = event.get("choices", [{}])[0].get("delta", {}) if event.get("choices") else {}
                            token = event.get("response") or delta.get("content")
                            if isinstance(token, str) and token:
                                yield "data: " + json.dumps({"content": token}, ensure_ascii=False) + "\n\n"
                        except (ValueError, KeyError, IndexError, TypeError):
                            continue
                    yield "data: [DONE]\n\n"
                except requests.RequestException:
                    yield "data: " + json.dumps({"error": "A conexão com o modelo foi interrompida."}, ensure_ascii=False) + "\n\n"
                finally:
                    upstream.close()
            return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        data = upstream.json()
        result_data = data.get("result", data) if isinstance(data, dict) else {}
        content = result_data.get("response") if isinstance(result_data, dict) else None
        if not isinstance(content, str) and isinstance(data, dict) and data.get("choices"):
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            app.logger.warning("Cloudflare response did not contain text content")
            return jsonify({"error": "O provedor retornou uma resposta vazia ou em formato inesperado."}), 502
        return jsonify({"content": content, "model": CF_MODEL, "provider": "Cloudflare Workers AI"})
    except requests.Timeout:
        return jsonify({"error": "O modelo demorou demais para responder. Tente novamente."}), 504
    except requests.RequestException:
        app.logger.exception("Network error while contacting Cloudflare Workers AI")
        return jsonify({"error": "Não foi possível conectar ao provedor de IA."}), 502
    except (ValueError, KeyError, IndexError, TypeError):
        app.logger.exception("Unexpected response from Cloudflare Workers AI")
        return jsonify({"error": "A resposta do provedor veio em um formato inesperado."}), 502

@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "A solicitação é grande demais."}), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

# Singularity

Aplicação web de chat com backend Flask e integração com Cloudflare Workers AI.

## Arquivos
- `index.html`: interface web responsiva em português.
- `app.py`: API Flask (`POST /api/chat`) e verificação de saúde (`/health`).
- `requirements.txt`: dependências Python.
- `render.yaml`: configuração para implantação no Render.
- `.env.example`: nomes das variáveis necessárias, sem credenciais reais.

## Publicação no Render
1. Crie um serviço Blueprint a partir deste repositório e da branch `main`.
2. No painel Environment do serviço, configure `CLOUDFLARE_ACCOUNT_ID` e `CLOUDFLARE_API_TOKEN`.
3. Nunca coloque tokens ou IDs secretos em arquivos do repositório, no HTML ou em mensagens.
4. O serviço usa `render.yaml`; o Render instalará as dependências e iniciará Gunicorn.

O modelo configurado por padrão é `@cf/qwen/qwen3.8-27b`; confirme no painel/documentação da Cloudflare se ele está disponível para sua conta antes de usar. É possível alterar `CF_MODEL` nas variáveis de ambiente.

## Endpoints
- `GET /`: interface web.
- `GET /health`: estado da configuração.
- `POST /api/chat`: recebe JSON com `message`, opcionalmente `history`, `mode` e `stream`.

## Segurança e limitações
As credenciais são lidas exclusivamente do ambiente do serviço. O backend aplica limite básico por IP, limite de tamanho e cabeçalhos de segurança. O limitador é em memória e não substitui autenticação nem proteção compartilhada para uso público em escala. A interface é um chat básico; anexos, pesquisa web, biblioteca e agentes não estão implementados.

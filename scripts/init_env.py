"""Create project-only secrets without replacing an existing .env or printing secrets."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / '.env'
existing = target.read_text(encoding='utf-8-sig') if target.exists() else ''
keys = {line.split('=', 1)[0].strip() for line in existing.splitlines() if '=' in line and not line.startswith('#')}
values = dict(POSTGRES_PASSWORD=secrets.token_hex(24), SERVICE_TOKEN=secrets.token_hex(32),
              WEBHOOK_TOKEN=secrets.token_hex(32), APP_SECRET=secrets.token_hex(32),
              ADMIN_EMAIL='admin@local.test', ADMIN_PASSWORD=secrets.token_urlsafe(20),
              N8N_ENCRYPTION_KEY=secrets.token_hex(32), QDRANT_API_KEY=secrets.token_hex(32),
              LLM_MODEL='qwen3:4b-instruct-2507-q4_K_M', EMBEDDING_MODEL='qwen3-embedding:0.6b',
              ASR_MODEL='/models/whisper-small', SPEAKER_MODEL='/models/speaker.onnx', N8N_URL='http://n8n:5678')
with target.open('a', encoding='utf-8') as out:
    out.write('\n' + '\n'.join(f'{key}={value}' for key, value in values.items() if key not in keys) + '\n')
print('Local .env prepared. Existing settings preserved. Secrets not printed.')

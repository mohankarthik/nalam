# Nalam - family health record pipeline, run as a scheduled container.
# supercronic fires drive->paperless sync, extraction and med reconciliation
# on a baked crontab. Same shape as gajana on purpose (see CLAUDE.md).
#
# Python base + tzdata + supercronic live in cron-base:local (shared with
# gajana) -- see homelab/base-images/cron-base. deploy_nalam.yml builds it
# before this image.
FROM cron-base:local

WORKDIR /app

# Install Python deps first for layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code + medical-knowledge configs baked in (no identities -- see
# CLAUDE.md's committed/gitignored table); personal data (secrets/,
# data/settings.json, data/people.json, data/analytes.json, data/health.db,
# data/state/, data/llm/, plugins/telegram_bot/settings.json) is bind-mounted
# at runtime (see .dockerignore).
COPY run_sync.py run_extract.py run_extract_queue.py run_meds.py run_telegram_bot.py ./
COPY src/ ./src/
COPY tools/ ./tools/
COPY plugins/ ./plugins/
COPY data/aliases.json data/units.json data/drugs.json data/analytes_extra.json data/specialties.json data/conditions.json ./data/
COPY data/configs/ ./data/configs/
COPY crontab ./crontab

CMD ["supercronic", "/app/crontab"]

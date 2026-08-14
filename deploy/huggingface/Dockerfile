# Hugging Face Spaces（Docker SDK）用。
# 公開版は閲覧専用で動かす（app_public.py が読み取り専用ガードを被せる）。
FROM python:3.11-slim

# trimesh / PyMuPDF / Pillow が必要とする最小限のライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Spaces は uid 1000 で動く。書き込み先を用意しておく。
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR $HOME/app

COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Spaces の公開ポート
EXPOSE 7860
ENV PORT=7860

CMD ["python", "app_public.py"]

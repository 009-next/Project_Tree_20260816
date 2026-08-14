"""公開URL（Hugging Face Spaces）用の閲覧専用ガード。

このアプリは本来 127.0.0.1 でのみ動かす前提で作ってあり、
API に認証を掛けていない。公開先では誰でも触れてしまうため、
「読むだけ」に絞る層をここで足す。

方針:
  既存のコードは一切書き換えない。app_public.py から
  add_middleware() でこの層を後から被せるだけにする。
  ローカル実行・exe 実行はこの層を通らないので、機能は従来どおり全部使える。

止めるもの:
  - GET / HEAD / OPTIONS 以外のすべて（アップロード・設定変更・LLM実行・編集）
  - GET だが実際に API を呼んで課金される疎通確認の 2 本

通すもの:
  - 画面表示、案件・段階・記録の閲覧、イメージ図と 2D/3D モデルの表示、
    資料出力（md / pdf / word / pptx / xlsx。いずれも台帳から組み立てるだけで LLM を呼ばない）
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

# GET だが LLM を実際に呼ぶため、公開先では止める
BLOCKED_GET_PATHS = {
    "/api/config/provider/probe",
    "/api/config/models/probe_alt",
}

ALLOWED_METHODS = {"GET", "HEAD", "OPTIONS"}

MESSAGE = (
    "この公開URLは閲覧専用です。"
    "資料の取り込み・LLMの実行・データの編集は無効にしています。"
    "すべての機能を試す場合は、READMEの手順でお手元の環境に導入してください。"
)

# 画面下に出す帯。判定に迷わないよう、公開版であることを明示する。
BANNER = """
<div id="pt-readonly-banner" style="
  position:fixed; left:0; right:0; bottom:0; z-index:99999;
  background:#C0392B; color:#fff; font-size:13px; line-height:1.5;
  padding:8px 14px; text-align:center;
  font-family:'Yu Gothic UI','Meiryo',sans-serif;">
  <b>閲覧専用の公開版です。</b>
  同梱データの閲覧・資料出力・2D/3Dモデルの操作はすべてお試しいただけます。
  取り込みとLLM実行は無効です（全機能はREADMEの手順でローカル導入してください）。
</div>
"""


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """書き込み系と課金系を止め、HTML には公開版である旨の帯を差し込む。"""

    async def dispatch(self, request, call_next):
        if request.method not in ALLOWED_METHODS:
            return JSONResponse(status_code=403,
                                content={"detail": MESSAGE, "readonly": True})
        if request.url.path in BLOCKED_GET_PATHS:
            return JSONResponse(status_code=403,
                                content={"detail": MESSAGE, "readonly": True})

        response = await call_next(request)

        ctype = response.headers.get("content-type", "")
        if not ctype.startswith("text/html"):
            return response

        # HTML のときだけ本文を読み直して帯を足す
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(status_code=500, content={"detail": "decode error"})

        if "</body>" in html:
            html = html.replace("</body>", BANNER + "</body>", 1)
        else:
            html += BANNER

        data = html.encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        headers.pop("content-encoding", None)
        return Response(content=data, status_code=response.status_code,
                        headers=headers, media_type="text/html; charset=utf-8")

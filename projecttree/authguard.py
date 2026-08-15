"""ローカル起動時のAPI保護（起動ごとのトークン + Cookie）。

これまでの防御は 127.0.0.1 バインドだけだった。同じPCで動く別のプロセスや、
悪意のあるWebページからの呼び出し（ブラウザ経由で 127.0.0.1 を叩く手口）は
素通りしてしまう。security.py には起動ごとのトークン機構が最初からあるのに、
どのエンドポイントにも適用されていなかったので、ここで被せる。

画面側（projecttree.html）は変更しない:
  最初にブラウザを開くとき URL に ?pt_token=... を付ける。
  この層がそれを見て Cookie を発行し、以後は fetch が同一オリジンの Cookie を
  自動で送るため、画面の JavaScript には手を入れなくてよい。

止めるもの:
  - Host が 127.0.0.1 / localhost 以外（DNS リバインディング対策）
  - トークンを持たない要求すべて（401）

通すもの:
  - Cookie / Authorization: Bearer / ?pt_token= のいずれかで正しいトークンを示した要求

既定では有効。無効にしたいときは環境変数 PT_NO_AUTH=1 を立てる
（従来どおり誰でも叩ける状態に戻る。検証用）。
"""

from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

COOKIE_NAME = "pt_session"
QUERY_NAME = "pt_token"

MESSAGE = (
    "このアプリはローカル専用です。アプリが開いたブラウザのタブから操作してください。"
    "（起動時に表示されるURLを使うと入れます）"
)


def disabled() -> bool:
    return os.environ.get("PT_NO_AUTH") == "1"


class AuthGuardMiddleware(BaseHTTPMiddleware):
    """起動ごとのトークンを持たない要求を止める。"""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    def _ok(self, presented: str | None) -> bool:
        if not presented:
            return False
        import secrets
        if presented.startswith("Bearer "):
            presented = presented[7:]
        return secrets.compare_digest(presented, self._token)

    async def dispatch(self, request, call_next):
        from projecttree import security as _sec

        # 1) Host を localhost 系に限定する
        if not _sec.host_allowed(request.headers.get("host")):
            return JSONResponse(status_code=403,
                                content={"detail": "このホスト名では利用できません。"})

        # 2) URL にトークンが付いていれば Cookie を発行して、付きでないURLへ戻す
        qtoken = request.query_params.get(QUERY_NAME)
        if qtoken and self._ok(qtoken):
            params = [(k, v) for k, v in request.query_params.multi_items()
                      if k != QUERY_NAME]
            url = request.url.path
            if params:
                from urllib.parse import urlencode
                url += "?" + urlencode(params)
            resp = RedirectResponse(url, status_code=303)
            resp.set_cookie(COOKIE_NAME, self._token, httponly=True,
                            samesite="strict", path="/")
            return resp

        # 3) Cookie か Authorization ヘッダで確認する
        if self._ok(request.cookies.get(COOKIE_NAME)) or \
           self._ok(request.headers.get("authorization")):
            return await call_next(request)

        return JSONResponse(status_code=401,
                            content={"detail": MESSAGE, "auth_required": True})


def install(app, token: str) -> bool:
    """アプリへ認証層を被せる。無効化されていれば何もしない。"""
    if disabled():
        return False
    app.add_middleware(AuthGuardMiddleware, token=token)
    return True

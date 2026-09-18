# 仕組み解説

このツールが何をどの順でやっているか、なぜその作りにしたかを説明します。
利用方法は [README.md](README.md) を参照してください。

## 全体像

合格発表は「発表ページに合格者一覧の PDF へのリンクが貼られる」形式を想定しています。
そのため処理は 2 段になります。

```
発表ページの URL
      │
      ├─ ① HTML を取得して、ページ内のリンクを全部拾う
      │        collect_links()
      │
      ▼
   リンク一覧 ──[利用者が選ぶ]──▶ 合格者一覧 PDF の URL
                                        │
                                        ├─ ② PDF を取得して全ページを走査
                                        │        search_pdf()
                                        ▼
                                  合格 / 要確認 / 該当なし / 照会不能
```

## ファイルの役割

| ファイル | 役割 |
| --- | --- |
| `app.py` | 全処理。取得・解析・照会・描画を節に分けて 1 ファイルに収めている |
| `style.css` | 掲示物としての体裁。`render_header()` が読み込んで `<style>` として注入する |
| `.streamlit/config.toml` | 本文フォント（明朝）とテーマ色。**CSS ではなくここで指定している**（理由は後述） |
| `run.sh` | 起動用。仮想環境が無ければ作り、初回のメール入力プロンプトも抑止する |

## `app.py` の構造

3 つの節に分かれています。

```
データ構造   Link / Hit / SearchResult      ── 素のデータ
取得まわり   _request, fetch_html,          ── Streamlit に依存しない
             fetch_pdf, collect_links
照会まわり   search_pdf, verdict_of         ── Streamlit に依存しない
─────────────────────────────────────────────────────────
画面         render_*, load_page,           ── ここだけ st に依存
             choose_from_links, main
```

上 3 節は `st` を import せずとも成立する純粋な関数で、キャッシュの付与も画面節で行います。

```python
# 関数自体はデコレータを持たない
def fetch_html(url: str) -> tuple[bytes, str | None, str, bool]: ...

# 画面節でキャッシュを被せる（CACHE_TTL = 300 / CACHE_ENTRIES = 8）
cached_fetch_html = st.cache_data(
    show_spinner=False, ttl=CACHE_TTL, max_entries=CACHE_ENTRIES
)(fetch_html)
```

キャッシュの有効期限や件数は「画面の都合」であって HTTP 取得の性質ではないため、
取得層に埋め込んでいません。おかげで `import app` して `fetch_html` や `search_pdf` を
単体で呼べます。末尾は `if __name__ == "__main__": main()` で守ってあるので、
import しても画面描画は走りません。

## 判定ロジック

ここがこのツールの中心です。**部分一致を合格と呼ばない**ことを最優先にしています。

`search_pdf()` は PDF の各ページについて次を行います。

1. `extract_text()` でテキストを取り出す
2. `unicodedata.normalize("NFKC", ...)` で全角・半角を吸収する
   （利用者が `１０５７` と入力しても `1057` と照合できる）
3. 英数字とハイフン以外を区切りとみなしてトークンに割る
4. 受験番号が**トークンとして**存在すれば完全一致（`exact`）
5. トークンではないが、空白を除いた文字列に含まれていれば部分一致（`loose`）

```python
TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\-]+")
...
tokens = {token for token in TOKEN_SPLIT.split(text) if token}
if target in tokens:
    exact.append(...)
elif target_compact and target_compact in compact(text):
    loose.append(...)
```

### なぜ 2 段階なのか

単純な部分文字列検索（`if search_string in text`）だと、受験番号 `105` が
他人の番号 `1057` に一致して「合格」と表示されます。合否判定でこれは許容できません。

一方、部分一致を単純に「不合格」としてしまうのも誤りです。PDF から文字を取り出すと、
多段組みのレイアウトなどで番号どうしの区切りが失われることがあります。

```
区切りが残る場合  '1057\n1073\n1096'      → 1057 はトークン ⇒ exact
区切りが消えた場合 '10571073109611011102'  → 1057 はトークンでない ⇒ loose
```

後者は本当は合格です。そのため `loose` は「要確認」として、該当箇所の抜粋とともに
利用者に判断を委ねます。

### 判定の対応表

| 状態 | 画面表示 |
| --- | --- |
| `exact` あり | 合格 |
| `loose` のみ | 要確認 |
| どちらも無く `text_chars == 0` | 照会不能（画像 PDF の可能性） |
| どちらも無く文字は取れている | 該当なし |

`text_chars` は全ページの抽出文字数の合計です。これが 0 なら「番号が無い」のではなく
「そもそも文字を読めていない」ので、黙って不合格と出さずに区別します。
この判定のために、途中で一致しても**全ページを走査します**。

## 落とし穴と、その対処

開発中に踏んだものです。同種の実装をする人向けに残します。

### 1. 文字コード：`apparent_encoding` を信じない

発表ページの多くは `Content-Type: text/html` に charset を書いていません。
このとき requests は HTTP/1.1 の既定値に従って `ISO-8859-1` を割り当てます。
これは「規格上の既定値」であって「そのページの文字コード」ではありません。

さらに `response.apparent_encoding`（推定）は `None` を返すことがあります。
実際に東大薬学系研究科のページで `None` が返り、フォールバックが `ISO-8859-1` を
採用して `修士課程合格者` が `ä¿®å£«èª²ç¨` に化けました。HTML 側には
`<meta charset="utf-8">` があったのに、どの経路もそれを見ていなかったのが原因です。

対処は、**自分でデコードしないこと**です。生バイトのまま BeautifulSoup に渡すと、
BOM → `meta charset` → 推定 の順に判定してくれます。

```python
def fetch_html(url: str) -> tuple[bytes, str | None, str, bool]:
    response, insecure = _request(url)
    return response.content, _declared_charset(response), response.url, insecure
    #      ^^^^^^^^^^^^^^^^^ .text ではなく .content

soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
```

HTTP ヘッダの charset は**明示されているときだけ**採用します。その判定には
正規表現ではなく標準ライブラリを使っています（`charset="utf-8"` のような
引用符付きの指定も正しく扱えるため）。

```python
def _declared_charset(response: requests.Response) -> str | None:
    message = Message()                                  # email.message.Message
    message["content-type"] = response.headers.get("content-type", "")
    return message.get_content_charset()                 # 未指定なら None
```

なお `requests.utils.get_encoding_from_headers()` は charset 未指定の `text/*` に
`ISO-8859-1` を返すので、ここでは使えません。

### 2. フォント：CSS で全体に指定しない

Streamlit のアイコンは Material Symbols の**合字**で描かれています。
`arrow_drop_down` という文字列を専用フォントで表示すると ▾ の字形になる仕組みです。

CSS で `[class^="st-"]` のように全要素へ `font-family` を指定すると、この
アイコン用フォントまで上書きされ、合字が成立せずに `arrow_drop_down` という
**文字列がそのまま描画されて隣のラベルに重なります**。

`!important` でアイコンだけ戻すこともできますが、それは Streamlit 内部の
`data-testid` に依存する特例で、更新で名前が変われば黙って壊れます。
本文フォントは Streamlit 自身の仕組みで指定するのが正解です。

```toml
# .streamlit/config.toml
[theme]
font = '"Hiragino Mincho ProN", "Yu Mincho", YuMincho, ... , serif'
```

この指定は「コードブロックを除くすべてのテキスト」に適用され、アイコンには
触れません。そのため `style.css` には `font-family` の全体指定がありません。

### 3. 折り返し：置き場所ごとに足さない

日本語ファイル名の PDF は URL が `%EF%BC%92...` 形式になり、実測で 233 文字・
うち 171 文字が区切りなしで連続していました。これは折り返せず枠外にはみ出します。

はみ出す文字列（URL・ファイル名・PDF の抜粋）はすべて外部由来で、どこに表示しても
長くなりえます。つまりこれは特定要素の性質ではなくページ全体の性質なので、
**1 か所だけで指定します**。

```css
.stApp { overflow-wrap: anywhere; }
```

`break-all` を全体に広げてはいけません。英単語を常に途中で割ってしまいます。
`anywhere` は「他に手段がないときだけ折る」ので、和文と英文の見え方は変わりません。

### 4. PDF でないものを掴まされる

拡張子が `.pdf` でも、掲示が移動・削除されていればサーバーは HTML のエラーページを
ステータス 200 で返します。これをそのまま pypdf に渡すと
`PdfStreamError: Stream has ended unexpectedly` という、利用者には意味不明な
英語の例外になります。

`fetch_pdf()` で先頭バイトと `Content-Length` を検査し、原因ごとに日本語へ変換します。

```python
if expected.isdigit() and len(data) < int(expected):
    raise InquiryError("ファイルの取得が途中で終わりました…")
if not data.startswith(b"%PDF"):
    raise InquiryError("…PDF ではなく Web ページでした…")
```

`InquiryError` は「原因と対処を日本語で説明できる、利用者向けのエラー」を表す型で、
画面側は次のように出し分けます。

```python
def report_failure(message: str, exc: Exception) -> None:
    st.error(str(exc) if isinstance(exc, InquiryError) else f"{message}（{exc}）")
```

### 5. SSL：常に検証を切らない

大学のサイトには証明書が正しく設定されていないものがあります。
ただし最初から `verify=False` にすると、健全なサイトでも検証を捨てることになります。

まず通常どおり検証し、`SSLError` が出たときだけ検証を省いて再試行し、
**その事実を画面に表示します**。

```python
try:
    response = requests.get(url, headers=headers, timeout=TIMEOUT)
except requests.exceptions.SSLError:
    response = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    insecure = True
```

## Streamlit 固有の注意

**スクリプトは操作のたびに上から再実行されます。** ウィジェットを 1 つ触るだけで
モジュール全体が再評価されるため、次の 2 点を意識しています。

- 通信結果は `st.cache_data` に載せる（`ttl=300`、`max_entries=8` で上限を設ける）
- 状態は `st.session_state` に置く。このアプリでは `page`（リンク一覧と文字コード）と
  `result`（照会結果）の 2 つだけ

`Link` のプロパティは `functools.cached_property` にしています。`selectbox` は
再実行のたびに全選択肢をラベル化するため、素の `property` だと毎回
`urlparse` を呼び直すことになります（リンク 300 件で実測 3.4ms → 0.01ms）。

**HTML の出力には `st.html` を使っています。** `st.markdown(..., unsafe_allow_html=True)`
と同じことができ、引数の書き忘れが起きません。内容は DOMPurify でサニタイズされますが、
`style.css` に使っている属性セレクタ（`div[data-testid="stExpander"]` など）は
そのまま通ることを実機で確認済みです。

## 手を入れるときに

- 判定ロジック（`search_pdf`、`verdict_of`）は `st` に依存しないので、
  `import app` して直接呼べば確かめられます。
- `style.css` は `runOnSave` の監視対象外です。CSS だけ編集したときは
  ブラウザの再読み込みが必要です。
- 表示文言のうち免責文は `DISCLAIMER` / `LIABILITY` に定数化してあります。
  2 か所（サイドバーと結果末尾）で食い違わないようにするためです。

"""入学試験 合格者受験番号 照会（Streamlit 版）

合格発表ページに掲示された PDF を取得し、
指定された受験番号が合格者一覧に記載されているかを照会する。

取得・解析・照会（この節）は Streamlit に依存しない純粋な関数として書き、
キャッシュと描画は「画面」節にまとめている。
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from functools import cached_property
from html import escape
from pathlib import Path
from posixpath import basename
from urllib.parse import unquote, urljoin, urlparse

import requests
import streamlit as st
import urllib3
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # 古い環境向けのフォールバック
    from PyPDF2 import PdfReader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENT = "Mozilla/5.0 (compatible; GouhiHantei/1.0)"
TIMEOUT = 30
CACHE_TTL = 300
CACHE_ENTRIES = 8

STYLE_PATH = Path(__file__).parent / "style.css"

# 受験番号として扱う文字（英数字とハイフン）以外を区切りとみなす
TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\-]+")
WHITESPACE = re.compile(r"\s+")

METHOD_FROM_PAGE = "発表ページから選択する"
METHOD_DIRECT = "PDF の所在を直接指定する"

DISCLAIMER = (
    "本照会の結果は参考情報です。"
    "正式な合否は必ず大学の公式発表によりご確認ください。"
)
LIABILITY = (
    "本照会の結果が実際の合否と異なっていた場合に生じる不利益について、"
    "作成者は責任を負いかねます。"
)
INSECURE_NOTICE = (
    "このサイトの SSL 証明書を検証できなかったため、"
    "検証を省略して取得しました。"
)


class InquiryError(Exception):
    """原因と対処を日本語で説明できる、利用者向けのエラー。"""


# ---------------------------------------------------------------- データ構造

def filename_of(url: str) -> str:
    """URL 末尾のファイル名を、%エンコードを戻した形で返す。"""
    return unquote(basename(urlparse(url).path))


@dataclass
class Link:
    text: str
    url: str

    @cached_property
    def is_pdf(self) -> bool:
        return urlparse(self.url).path.lower().endswith(".pdf")

    @cached_property
    def filename(self) -> str:
        return filename_of(self.url) or self.url

    @cached_property
    def label(self) -> str:
        kind = "PDF" if self.is_pdf else "HTML"
        text = self.text if len(self.text) <= 68 else self.text[:67] + "…"
        return f"［{kind}］{text}　—　{self.filename}"


@dataclass
class Hit:
    page: int
    context: str


@dataclass
class SearchResult:
    exact: list[Hit]
    loose: list[Hit]
    page_count: int
    text_chars: int


# ---------------------------------------------------------------- 取得まわり

def _request(url: str) -> tuple[requests.Response, bool]:
    """URL を取得する。証明書エラーのときだけ検証を省いて再試行する。"""
    headers = {"User-Agent": USER_AGENT}
    insecure = False
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.SSLError:
        response = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
        insecure = True
    response.raise_for_status()
    return response, insecure


def _declared_charset(response: requests.Response) -> str | None:
    """HTTP ヘッダが明示している文字コードを返す。

    charset の指定がない text/* に requests は ISO-8859-1 を割り当てるが、
    これは HTTP/1.1 の既定値にすぎず実際の文字コードとは限らない。
    そのため「明示されている場合だけ」採用する。
    """
    message = Message()
    message["content-type"] = response.headers.get("content-type", "")
    return message.get_content_charset()


def fetch_html(url: str) -> tuple[bytes, str | None, str, bool]:
    """HTML を生バイトのまま返す。デコードは BeautifulSoup に任せる。"""
    response, insecure = _request(url)
    return response.content, _declared_charset(response), response.url, insecure


def fetch_pdf(url: str) -> tuple[bytes, bool]:
    """PDF を取得する。PDF でないもの・途中で切れたものはここで弾く。"""
    response, insecure = _request(url)
    data = response.content

    expected = response.headers.get("content-length", "")
    if expected.isdigit() and len(data) < int(expected):
        raise InquiryError(
            f"ファイルの取得が途中で終わりました（{len(data):,} / {int(expected):,} バイト）。"
            "通信状況をご確認のうえ、もう一度お試しください。"
        )

    if not data.startswith(b"%PDF"):
        kind = (response.headers.get("content-type") or "不明").split(";")[0].strip()
        if data.lstrip()[:1] == b"<":
            raise InquiryError(
                "指定された所在から返ってきたのは PDF ではなく Web ページでした"
                f"（Content-Type: {kind}）。PDF 以外のリンクを選んでいないか、"
                "掲示が移動・削除されていないかをご確認ください。"
            )
        raise InquiryError(
            f"指定された所在のファイルは PDF ではありません（Content-Type: {kind}）。"
        )

    return data, insecure


def collect_links(
    html: bytes, base_url: str, charset: str | None = None
) -> tuple[list[Link], str | None]:
    """リンクを集める。あわせて実際に使われた文字コードを返す。

    生バイトを渡すと BeautifulSoup が BOM・meta charset・推定の順に
    判定してくれるので、<meta charset="utf-8"> を取りこぼさない。
    """
    soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
    links: list[Link] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue

        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)

        text = " ".join(anchor.get_text(" ", strip=True).split())
        links.append(Link(text or "（リンクテキストなし）", url))

    return links, soup.original_encoding


def filter_links(links: list[Link], keyword: str) -> list[Link]:
    """空白区切りのキーワードをすべて含むリンクだけに絞る。"""
    for word in keyword.lower().split():
        links = [
            link
            for link in links
            if word in link.text.lower() or word in link.url.lower()
        ]
    return links


# ---------------------------------------------------------------- 照会まわり

def normalize(text: str) -> str:
    """全角・半角の違いを吸収する。"""
    return unicodedata.normalize("NFKC", text)


def compact(text: str) -> str:
    """空白をすべて取り除く。"""
    return WHITESPACE.sub("", text)


def context_of(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return " ".join(line.split())[:200]
    return ""


def search_pdf(pdf_bytes: bytes, exam_number: str) -> SearchResult:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise InquiryError(
            "PDF を読み取れませんでした。ファイルが壊れているか、"
            f"途中までしか取得できていない可能性があります（{exc}）。"
        ) from exc

    target = normalize(exam_number).strip()
    target_compact = compact(target)

    exact: list[Hit] = []
    loose: list[Hit] = []
    text_chars = 0

    for page_number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:  # 壊れたページは飛ばして続行する
            raw = ""

        text_chars += len(raw.strip())
        text = normalize(raw)
        tokens = {token for token in TOKEN_SPLIT.split(text) if token}

        if target in tokens:
            exact.append(Hit(page_number, context_of(text, target)))
        elif target_compact and target_compact in compact(text):
            loose.append(Hit(page_number, context_of(text, target)))

    return SearchResult(exact, loose, len(reader.pages), text_chars)


def pages_of(hits: list[Hit]) -> str:
    return "・".join(str(hit.page) for hit in hits)


def verdict_of(result: SearchResult, number: str) -> tuple[str, str, str, str]:
    """照会結果を（種別, 見出し, 本文, 補足）に落とす。"""
    if result.exact:
        return (
            "pass",
            "合格",
            f"受験番号 <b>{number}</b> は合格者一覧に記載されています。",
            f"該当箇所：第 {pages_of(result.exact)} ページ",
        )
    if result.loose:
        return (
            "caution",
            "要確認",
            f"受験番号 <b>{number}</b> は、独立した番号としては確認できませんでした。",
            f"第 {pages_of(result.loose)} ページに同じ並びの数字が含まれています。"
            "他の受験番号の一部を検出した可能性があるため、"
            "下記の該当箇所を必ずご確認ください。",
        )
    if result.text_chars == 0:
        return (
            "caution",
            "照会不能",
            "この PDF からは文字を取り出せませんでした。",
            "画像として取り込まれた PDF である可能性があります。"
            "PDF を直接ご確認ください。",
        )
    return (
        "fail",
        "該当なし",
        f"受験番号 <b>{number}</b> は合格者一覧に記載されていません。",
        f"全 {result.page_count} ページを照会しました。",
    )


# ---------------------------------------------------------------- 画面

cached_fetch_html = st.cache_data(
    show_spinner=False, ttl=CACHE_TTL, max_entries=CACHE_ENTRIES
)(fetch_html)
cached_fetch_pdf = st.cache_data(
    show_spinner=False, ttl=CACHE_TTL, max_entries=CACHE_ENTRIES
)(fetch_pdf)


def section(number: str, title: str) -> None:
    st.html(
        f'<div class="section"><span class="num">{number}</span>'
        f'<span class="ttl">{title}</span></div>'
    )


def verdict(kind: str, main: str, note: str, sub: str = "") -> None:
    st.html(
        f'<div class="verdict {kind}">'
        '<div class="cap">照 会 結 果</div>'
        f'<div class="main">{main}</div>'
        f'<div class="note">{note}</div>'
        + (f'<div class="sub">{sub}</div>' if sub else "")
        + "</div>"
    )


def report_failure(message: str, exc: Exception) -> None:
    """例外を画面向けの文言にして出す。"""
    st.error(str(exc) if isinstance(exc, InquiryError) else f"{message}（{exc}）")


def render_header() -> None:
    st.html(f"<style>{STYLE_PATH.read_text(encoding='utf-8')}</style>")
    st.html(
        '<div class="notice-head">'
        '<div class="kicker">入学試験</div>'
        "<h1>合格者受験番号 照会</h1>"
        '<div class="en">Examination Result Inquiry</div>'
        '<div class="rule"></div>'
        "</div>"
        '<div class="lead">合格発表のページに掲示された合格者一覧（PDF）を取得し、'
        "指定された受験番号が記載されているかを照会します。</div>"
    )


def render_sidebar() -> None:
    with st.sidebar:
        section("案内", "照会の手順")
        st.html(
            '<div class="note-small">'
            "一、合格発表ページの所在（URL）を入力し、発表ページを読み込む<br>"
            "二、掲示されている合格者一覧の PDF を選択する<br>"
            "三、受験番号を入力し、照会する<br><br>"
            "合格者一覧の PDF の所在が判明している場合は、"
            f"「{METHOD_DIRECT}」に切り替えてください。"
            "</div>"
        )
        st.markdown("---")
        section("注意", "ご利用にあたって")
        st.html(f'<div class="note-small">{DISCLAIMER}{LIABILITY}</div>')


def load_page(page_url: str) -> None:
    """発表ページを読み込み、集めたリンクを session_state に置く。"""
    if not page_url.strip():
        st.warning("URL が入力されていません。")
        return

    try:
        with st.spinner("発表ページを読み込んでいます…"):
            html, charset, final_url, insecure = cached_fetch_html(page_url.strip())
            links, encoding = collect_links(html, final_url, charset)
    except Exception as exc:
        st.session_state.page = None
        report_failure("発表ページを読み込めませんでした", exc)
        return

    st.session_state.page = {"links": links, "encoding": encoding}
    st.session_state.result = None
    if insecure:
        st.warning(INSECURE_NOTICE)


def choose_from_links() -> str:
    """集めたリンクから合格者一覧を選ばせ、その URL を返す。"""
    page = st.session_state.page
    if not page:
        return ""

    section("二", "合格者一覧の選択")
    links: list[Link] = page["links"]
    pdf_links = [link for link in links if link.is_pdf]

    if not pdf_links:
        st.info(
            "拡張子が .pdf の掲示は見つかりませんでした。"
            "ページ内のすべてのリンクから選択してください。"
        )

    show_all = st.checkbox(
        "PDF 以外のリンクも表示する", value=not pdf_links, disabled=not pdf_links
    )
    keyword = st.text_input(
        "絞り込み（空白区切り。すべてを含むものを表示）",
        placeholder="修士課程合格者 2026.10",
    )
    candidates = filter_links(links if show_all else pdf_links, keyword)

    encoding = page["encoding"]
    st.html(
        f'<div class="note-small">掲示リンク {len(links)} 件中、該当 {len(candidates)} 件'
        + (f"（文字コード: {encoding}）" if encoding else "")
        + "</div>"
    )

    if not candidates:
        st.warning("条件に該当する掲示がありません。絞り込みを緩めてください。")
        return ""

    chosen = st.selectbox(
        "合格者一覧",
        candidates,
        format_func=lambda link: link.label,
        label_visibility="collapsed",
    )
    return chosen.url


def select_from_page() -> str:
    section("一", "合格発表ページの所在")
    page_url = st.text_input(
        "発表ページの URL",
        placeholder="https://www.example.ac.jp/exam/index.html",
        help="合格者一覧の PDF が掲示されているページの URL を入力してください。",
    )
    if st.button("発表ページを読み込む", use_container_width=True):
        load_page(page_url)
    return choose_from_links()


def enter_pdf_url() -> str:
    section("一", "合格者一覧の所在")
    return st.text_input(
        "PDF の URL",
        placeholder="https://www.example.ac.jp/exam/2026_master.pdf",
    ).strip()


def run_inquiry(pdf_url: str, exam_number: str) -> None:
    """受験番号を照会し、結果を session_state に置く。"""
    if not pdf_url:
        st.warning("合格者一覧が指定されていません。")
        return
    if not exam_number.strip():
        st.warning("受験番号が入力されていません。")
        return

    try:
        with st.spinner("合格者一覧を照会しています…"):
            pdf_bytes, insecure = cached_fetch_pdf(pdf_url)
            result = search_pdf(pdf_bytes, exam_number)
    except Exception as exc:
        st.session_state.result = None
        report_failure("合格者一覧を照会できませんでした", exc)
        return

    st.session_state.result = {
        "result": result,
        "pdf_url": pdf_url,
        "pdf_bytes": pdf_bytes,
        "exam_number": exam_number.strip(),
        "insecure": insecure,
        "queried_at": datetime.now(),
    }


def render_result(state: dict) -> None:
    result: SearchResult = state["result"]
    number = escape(state["exam_number"])
    verdict(*verdict_of(result, number))

    if state["insecure"]:
        st.warning(INSECURE_NOTICE)

    filename = filename_of(state["pdf_url"]) or "result.pdf"

    section("明細", "照会内容")
    st.html(
        '<table class="detail">'
        f"<tr><th>照会日時</th><td>{state['queried_at']:%Y年%-m月%-d日 %-H時%M分}</td></tr>"
        f"<tr><th>受験番号</th><td>{number}</td></tr>"
        f"<tr><th>照会した掲示</th><td>{escape(filename)}</td></tr>"
        f"<tr><th>頁数</th><td>{result.page_count} ページ</td></tr>"
        f"<tr><th>抽出文字数</th><td>{result.text_chars:,} 字</td></tr>"
        "</table>"
    )

    hits = [hit for hit in result.exact + result.loose if hit.context]
    if hits:
        section("照合", "該当箇所の抜粋")
        for hit in hits:
            st.html(f'<div class="note-small">第 {hit.page} ページ</div>')
            st.code(hit.context, language=None)

    with st.expander("掲示された PDF を確認する"):
        st.html(
            f'<div class="note-small"><b>{escape(filename)}</b></div>'
            f'<div class="url-text">{escape(state["pdf_url"])}</div>'
        )
        st.download_button(
            "この PDF を保存する",
            data=state["pdf_bytes"],
            file_name=filename,
            mime="application/pdf",
        )

    st.html(f'<div class="note-small" style="margin-top:1.4rem">{DISCLAIMER}</div>')


def main() -> None:
    st.set_page_config(
        page_title="合格者受験番号 照会", page_icon="📋", layout="centered"
    )
    st.session_state.setdefault("page", None)
    st.session_state.setdefault("result", None)

    render_header()
    render_sidebar()

    method = st.radio("照会方法", [METHOD_FROM_PAGE, METHOD_DIRECT], horizontal=True)
    if method == METHOD_FROM_PAGE:
        pdf_url = select_from_page()
        number_section = "三"
    else:
        pdf_url = enter_pdf_url()
        number_section = "二"

    section(number_section, "受験番号の照会")
    exam_number = st.text_input(
        "受験番号",
        placeholder="1057",
        help="全角で入力しても半角に変換して照合します。",
    )

    if st.button("照 会 す る", type="primary", use_container_width=True):
        run_inquiry(pdf_url, exam_number)

    if st.session_state.result:
        render_result(st.session_state.result)


if __name__ == "__main__":
    main()

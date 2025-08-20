# streamlit_app.py
# -*- coding: utf-8 -*-
import os
import re
import io
import json
import time
import streamlit as st
import requests
import urllib3
from urllib.parse import urljoin, unquote, urlparse
from requests.utils import requote_uri
from bs4 import BeautifulSoup

st.set_page_config(page_title="메가스터디 크롤러 통합", layout="wide")

# ───────────── 공통 유틸 ─────────────
def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def normalize_paragraphs(raw_text: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def extract_idx_from_onclick(onclick: str) -> str | None:
    m = re.search(r"\((\d+)\)", onclick or "")
    return m.group(1) if m else None

def filename_from_cd(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*[^']+'[^']*'([^;]+)", content_disposition, flags=re.I)
    if m:
        try:
            return unquote(m.group(1))
        except Exception:
            return m.group(1)
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, flags=re.I)
    return m.group(1) if m else None

def safe_filename(name: str) -> str:
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        name = name.replace(ch, '_')
    return name.strip()

def parse_json_or_empty(s: str):
    s = (s or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception as e:
        st.warning(f"JSON 파싱 오류: {e}")
        return {}

def make_session(headers: dict, verify_ssl: bool):
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sess = requests.Session()
    sess.headers.update(headers or {})
    return sess

# ───────────── 엔드포인트 ─────────────
BASE = "https://www.megastudy.net"
ENDPOINTS = {
    "입시 리포트": {
        "LIST":   f"{BASE}/Entinfo/news/news_list_ax.asp",
        "DETAIL": f"{BASE}/Entinfo/news/news_view_ax.asp",
        "referer": f"{BASE}/Entinfo/news/news_list.asp",
        "params_list":   lambda page: {"page": str(page), "caty": "", "cat2": "", "searchType": "", "searchWord": ""},
        "params_detail": lambda idx, page: {"idx": idx, "caty": "", "cat2": "", "page": str(page), "searchType": "", "searchWord": ""},
    },
    "입시 뉴스": {
        "LIST":   f"{BASE}/Entinfo/ipsi_news/news_list_ax.asp",
        "DETAIL": f"{BASE}/Entinfo/ipsi_news/news_view_ax.asp",
        "referer": f"{BASE}/Entinfo/ipsi_news/news_list.asp",
        "params_list":   lambda page: {"page": str(page), "caty": "", "cat2": "", "searchType": "", "searchWord": ""},
        "params_detail": lambda idx, page: {"idx": idx, "caty": "", "cat2": "", "page": str(page), "searchType": "", "searchWord": ""},
    },
    "교육기관발표자료": {
        "LIST":     f"{BASE}/entinfo/g_archive/list_ax.asp",
        "DETAIL":   f"{BASE}/entinfo/g_archive/view_ax.asp",
        "referer":  f"{BASE}/entinfo/g_archive/list.asp",
        "viewpage": f"{BASE}/entinfo/g_archive/view.asp",  # 파일서버 Referer
        "params_list":   lambda page: {"page": str(page), "searchType": "", "searchWord": ""},
        "params_detail": lambda idx, page: {"idx": idx, "page": "1", "searchType": "", "searchWord": ""},
    },
}
def build_g_archive_detail_referer(idx: str) -> str:
    return f"{ENDPOINTS['교육기관발표자료']['viewpage']}?idx={idx}"

# ───────────── 크롤 함수 ─────────────
def fetch_list_page(sess, cookies, source_name, page, verify_ssl):
    url = ENDPOINTS[source_name]["LIST"]
    params = ENDPOINTS[source_name]["params_list"](page)
    r = sess.get(url, params=params, cookies=cookies, verify=verify_ssl, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows, seen = [], set()
    for cell in soup.select(".td_lft"):
        a = cell.select_one(".linkTxt")
        if not a: 
            continue
        idx = extract_idx_from_onclick(a.get("onclick"))
        if not idx or idx in seen:
            continue
        seen.add(idx)
        title = clean_spaces(' '.join(cell.stripped_strings))
        rows.append({"idx": idx, "title": title})
    return rows

def fetch_detail_body_and_soup(sess, cookies, source_name, idx, page_for_context, verify_ssl):
    url = ENDPOINTS[source_name]["DETAIL"]
    params = ENDPOINTS[source_name]["params_detail"](idx, page_for_context)
    r = sess.get(url, params=params, cookies=cookies, verify=verify_ssl, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for content in soup.select(".viewContents"):
        for bad in content(["script", "style"]):
            bad.decompose()
        raw = content.get_text(separator="\n", strip=True)
        norm = normalize_paragraphs(raw)
        if norm:
            parts.append(norm)
    return "\n\n".join(parts), soup

def find_attachments_from_g_archive(detail_soup):
    urls = []
    for a in detail_soup.select(".commonBoardView--items .viewpage_addfile a[href]"):
        href = a.get("href")
        if href:
            urls.append(urljoin(BASE, href))
    return urls

def download_binary(sess, cookies, url, verify_ssl, referer: str):
    safe_url = requote_uri(url)  # 한글/공백 인코딩
    headers = dict(sess.headers); headers["Referer"] = referer
    with sess.get(safe_url, cookies=cookies, headers=headers,
                  verify=verify_ssl, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        fname = filename_from_cd(resp.headers.get("Content-Disposition"))
        if not fname:
            path_name = unquote(os.path.basename(urlparse(resp.url).path))
            fname = path_name if path_name else "download.bin"
        fname = safe_filename(fname)
        bio = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=262144):
            if chunk:
                bio.write(chunk)
        bio.seek(0)
        return fname, bio

# ───────────── 세션 상태 ─────────────
S = st.session_state
for key, val in {
    "results": [],
    "config": {},
    "ran": False,
}.items():
    if key not in S:
        S[key] = val

# ───────────── 사이드바 ─────────────
st.sidebar.header("설정")
source_name = st.sidebar.selectbox("크롤 대상", ["입시 리포트", "입시 뉴스", "교육기관발표자료"])
start_page = st.sidebar.number_input("시작 페이지", min_value=1, value=1, step=1)
end_page   = st.sidebar.number_input("끝 페이지", min_value=start_page, value=start_page, step=1)

st.sidebar.markdown("**쿠키 JSON** (비워두면 빈 딕셔너리)")
cookies_json = st.sidebar.text_area("cookies", value="", height=120, placeholder='{"CK%5FUSER%5FINFO": "..."}')

default_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Accept": "text/html, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": ENDPOINTS[source_name]["referer"],
}
headers_json = st.sidebar.text_area("headers", value=json.dumps(default_headers, ensure_ascii=False, indent=2), height=160)

verify_ssl = st.sidebar.checkbox("SSL 검증 사용(권장)", value=False)
delay_sec  = st.sidebar.slider("요청 간 딜레이(초)", 0.0, 2.0, 0.4, 0.1)

# 교육기관발표자료 전용 옵션
prefetch=False; max_prefetch_mb=50; want_download=False; ext_filter=""
if source_name == "교육기관발표자료":
    st.sidebar.markdown("---"); st.sidebar.subheader("첨부 옵션")
    prefetch = st.sidebar.checkbox("미리 받아두기(원클릭)", value=False)
    max_prefetch_mb = st.sidebar.number_input("미리받기 최대 크기(MB)", 10, 500, 50, 10)
    want_download = st.sidebar.checkbox("선택 다운로드 섹션 표시", value=True)
    ext_filter    = st.sidebar.text_input("확장자 필터(쉼표, 비우면 전체)", value="zip,pdf")

# 실행 버튼
if st.sidebar.button("크롤 실행"):
    S.results = []
    S.config = {
        "source_name": source_name,
        "start_page": int(start_page),
        "end_page": int(end_page),
        "cookies": parse_json_or_empty(cookies_json),
        "headers": parse_json_or_empty(headers_json) or default_headers,
        "verify_ssl": bool(verify_ssl),
        "delay_sec": float(delay_sec),
        "prefetch": bool(prefetch),
        "max_prefetch_mb": int(max_prefetch_mb),
        "want_download": bool(want_download),
        "ext_filter": str(ext_filter),
    }
    S.ran = True

if st.sidebar.button("초기화"):
    S.results = []
    S.config = {}
    S.ran = False
    # 준비된 파일도 모두 제거
    for k in list(S.keys()):
        if k.startswith(("blob_", "name_")):
            del S[k]

# ───────────── 제목/캡션 ─────────────
cfg = S.config if S.ran else {"source_name": source_name, "start_page": start_page, "end_page": end_page}
st.title("메가스터디 크롤러 통합")
st.caption(f"대상: {cfg['source_name']} | 페이지: {cfg['start_page']} ~ {cfg['end_page']}")

# ───────────── 콜백: 첨부 준비 ─────────────
def prep_file(idx: str, ai: int, url: str):
    """버튼 콜백: 파일을 받아 세션 상태에 저장. 리런 후 바로 download_button 표시"""
    try:
        sess_local = make_session(S.config["headers"], S.config["verify_ssl"])
        ref = build_g_archive_detail_referer(idx) if S.config["source_name"] == "교육기관발표자료" else ENDPOINTS[S.config["source_name"]]["referer"]
        fname, data = download_binary(sess_local, S.config["cookies"], url, S.config["verify_ssl"], referer=ref)
        S[f"name_{idx}_{ai}"] = fname
        S[f"blob_{idx}_{ai}"] = data.getvalue()
    except Exception as e:
        st.warning(f"다운로드 준비 실패: {e}")

# ───────────── 한 건 렌더(즉시 출력) ─────────────
def render_one_row(row: dict, cfg: dict):
    idx, title, body = row["idx"], row["title"], row["body"]
    with st.expander(f"[{idx}] {title}", expanded=False):
        st.markdown(body.replace("\n", "  \n"))

        # 교육기관발표자료: 첨부 버튼들
        if cfg["source_name"] == "교육기관발표자료":
            atts = row.get("attachments") or []
            if not atts:
                st.write("(첨부 없음)")
                return
            st.markdown("**첨부파일:**")
            prefetched = row.get("prefetched") if cfg.get("prefetch") else None

            for ai, au in enumerate(atts):
                key_blob = f"blob_{idx}_{ai}"
                key_name = f"name_{idx}_{ai}"

                # 1) prefetch 성공 → 바로 다운로드 버튼
                if cfg.get("prefetch") and prefetched and ai < len(prefetched) and prefetched[ai].get("ok"):
                    fname = S.get(key_name, prefetched[ai]["name"])
                    st.download_button(
                        label=f"📥 {fname}",
                        data=S[key_blob],
                        file_name=fname,
                        mime="application/octet-stream",
                        key=f"dl_{idx}_{ai}"
                    )
                    continue

                # 2) 미리받기 미사용/실패 → 준비 버튼(콜백) + 준비되어 있으면 다운로드 버튼
                cols = st.columns([2, 6])
                with cols[0]:
                    st.button(
                        "📥 다운로드 준비",
                        key=f"prep_{idx}_{ai}",
                        on_click=prep_file,
                        args=(idx, ai, au),
                    )
                with cols[1]:
                    if key_blob in S and key_name in S:
                        st.download_button(
                            label=f"📥 {S[key_name]}",
                            data=S[key_blob],
                            file_name=S[key_name],
                            mime="application/octet-stream",
                            key=f"dl_ready_{idx}_{ai}"
                        )

# ───────────── 크롤 실행: 페이지/항목별 즉시 렌더 ─────────────
if S.ran and S.config:
    cookies = S.config["cookies"]; headers = S.config["headers"]
    verify_ssl = S.config["verify_ssl"]; delay = S.config["delay_sec"]
    prefetch = S.config.get("prefetch", False); max_mb = S.config.get("max_prefetch_mb", 50)

    sess = make_session(headers, verify_ssl)
    total_pages = S.config["end_page"] - S.config["start_page"] + 1
    pbar = st.progress(0, text="크롤링 중…"); done_pages = 0

    for page in range(S.config["start_page"], S.config["end_page"] + 1):
        st.write(f"### [page {page}]")
        items = fetch_list_page(sess, cookies, S.config["source_name"], page, verify_ssl)
        if not items:
            st.info("데이터 없음")
            done_pages += 1
            pbar.progress(int(done_pages/max(1,total_pages)*100), text=f"{done_pages}/{total_pages} 페이지 완료")
            continue

        for it in items:
            idx, title = it["idx"], it["title"]
            body, soup_detail = fetch_detail_body_and_soup(sess, cookies, S.config["source_name"], idx, page, verify_ssl)
            row = {"idx": idx, "title": title, "body": body}

            # 교육기관발표자료: 첨부/미리받기
            if S.config["source_name"] == "교육기관발표자료":
                attaches = find_attachments_from_g_archive(soup_detail)
                row["attachments"] = attaches
                if prefetch and attaches:
                    limit_bytes = max_mb * 1024 * 1024
                    row["prefetched"] = []
                    for ai, au in enumerate(attaches):
                        try:
                            # prefetch 시 세션에 저장 → 바로 버튼 렌더
                            fname, data = download_binary(sess, cookies, au, verify_ssl, referer=build_g_archive_detail_referer(idx))
                            if data.getbuffer().nbytes > limit_bytes:
                                row["prefetched"].append({"ok": False, "reason": "limit", "name": fname})
                                continue
                            key_blob = f"blob_{idx}_{ai}"; key_name = f"name_{idx}_{ai}"
                            S[key_name] = fname; S[key_blob] = data.getvalue()
                            row["prefetched"].append({"ok": True, "name": fname})
                        except Exception as e:
                            row["prefetched"].append({"ok": False, "reason": str(e)})

            # 즉시 렌더 + 누적
            render_one_row(row, S.config)
            S.results.append(row)
            time.sleep(delay)

        done_pages += 1
        pbar.progress(int(done_pages/max(1,total_pages)*100), text=f"{done_pages}/{total_pages} 페이지 완료")
        time.sleep(delay)

    st.success(f"총 {len(S.results)}건 수집 완료")
    st.dataframe([{"idx": r["idx"], "title": r["title"]} for r in S.results], use_container_width=True)

    # 선택 다운로드 섹션 (교육기관발표자료)
    if S.config["source_name"] == "교육기관발표자료" and S.config.get("want_download", False):
        st.markdown("---"); st.subheader("첨부파일 선택 다운로드")
        with_attach = [r for r in S.results if r.get("attachments")]
        if not with_attach:
            st.info("첨부가 있는 게시물이 없습니다.")
        else:
            options = [f"{r['idx']} | {r['title']}" for r in with_attach]
            selected_items = st.multiselect("다운로드할 게시물 선택", options, default=[])
            exts = [e.strip().lower() for e in (S.config.get("ext_filter") or "").split(",") if e.strip()]
            if st.button("선택 항목 다운로드 준비"):
                for opt in selected_items:
                    idx_str = opt.split("|", 1)[0].strip()
                    target = next((r for r in with_attach if str(r["idx"]) == idx_str), None)
                    if not target: 
                        continue
                    attaches = target.get("attachments", [])
                    for ai, au in enumerate(attaches):
                        if exts:
                            path = urlparse(au).path.lower()
                            if not any(path.endswith("." + x) for x in exts):
                                continue
                        try:
                            sess_local = make_session(headers, verify_ssl)
                            fname, data = download_binary(sess_local, cookies, au, verify_ssl, referer=build_g_archive_detail_referer(idx_str))
                            st.download_button(
                                label=f"📥 {fname}",
                                data=data,
                                file_name=fname,
                                mime="application/octet-stream",
                                key=f"bulk_dl_{idx_str}_{ai}"
                            )
                        except Exception as e:
                            st.warning(f"다운로드 실패: {e}")

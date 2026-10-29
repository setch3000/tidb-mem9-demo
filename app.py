"""진입점: 로그인 게이트, 언어 토글, 화면 이동(st.navigation), 사이드바(데모 사용자·초기화·고지)."""
import streamlit as st

from core.config import missing

st.set_page_config(page_title="TiDB + mem9 AI demo", page_icon="📈", layout="wide")

from ui.i18n import lang_toggle, t  # noqa: E402

if absent := missing():
    st.error(t("err.env_missing", keys=", ".join(absent)))
    st.stop()

from core import audit  # noqa: E402  (환경변수 확인 뒤 DB 연결)
from ui.common import current_user_id, persona_name, users  # noqa: E402

# ---- 로그인 게이트 ----
if not audit.auth_bypassed() and not audit.auth_configured():
    st.error(t("err.auth_missing"))     # 설정 실수로 보호 없이 열리지 않게 한다
    st.stop()

if not audit.is_logged_in():
    lang_toggle()
    st.title(t("app.title"))
    st.write(t("login.intro"))
    ip = audit.display_ip()
    st.info(t("login.ip", ip=ip) if ip else t("login.ip_none"))
    st.caption(t("login.notice"))
    audit.log_once("visit")
    if st.button(t("login.button"), type="primary", icon=":material/login:"):
        st.login()
    st.stop()

audit.log_once("login")
is_admin = audit.is_admin()

PAGES = [
    st.Page("app_pages/1_Signal_분석.py", title=t("nav.signal"), icon=":material/feed:", default=True),
    st.Page("app_pages/2_사용자_대시보드.py", title=t("nav.dashboard"), icon=":material/account_balance_wallet:"),
    st.Page("app_pages/3_개인화_추천.py", title=t("nav.recommend"), icon=":material/auto_awesome:"),
    st.Page("app_pages/4_승인_실행.py", title=t("nav.approval"), icon=":material/task_alt:"),
    st.Page("app_pages/5_Behind_the_scenes.py", title=t("nav.behind"), icon=":material/manage_search:"),
]
if is_admin:
    PAGES.append(st.Page("app_pages/6_접속_기록.py", title=t("nav.audit"), icon=":material/admin_panel_settings:"))
nav = st.navigation(PAGES)

with st.sidebar:
    st.markdown(f"### {t('app.sidebar_title')}")
    lang_toggle()
    people = users()
    ids = [u["id"] for u in people]
    names = {u["id"]: persona_name(u) for u in people}     # 표시 이름은 이번 실행의 언어로 미리 만든다
    st.session_state["user_id"] = st.selectbox(
        t("side.persona"), ids, index=ids.index(current_user_id()) if current_user_id() in ids else 0,
        format_func=names.get)

    if is_admin:
        with st.popover(t("side.reset"), icon=":material/restart_alt:"):
            st.write(t("side.reset_body"))
            if st.button(t("side.reset_run"), type="primary"):
                from scripts.seed import reset_demo

                audit.log_event("admin:reset")
                with st.spinner(t("side.reset_spinner")):
                    reset_demo()
                st.cache_data.clear()
                for key in list(st.session_state):
                    if key not in ("user_id", "lang") and not key.startswith("_audit"):
                        del st.session_state[key]
                st.success(t("side.reset_done"))
                st.rerun()

    st.divider()
    who = audit.identity()
    if audit.auth_bypassed():
        st.caption(t("login.dev_mode", email=who["email"]))
    else:
        st.caption(t("side.signed_in", email=who["email"]))
        if st.button(t("side.logout"), icon=":material/logout:"):
            audit.log_event("logout")
            st.logout()
    st.caption(t("disclaimer"))

st.title(nav.title)
st.caption(t("disclaimer"))
nav.run()

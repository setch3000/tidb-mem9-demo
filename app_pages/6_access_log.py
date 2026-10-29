"""⑥ 접속 기록 (관리자 전용): 로그인·LLM 사용 기록을 TiDB access_logs에서 집계."""
import csv
import io

import streamlit as st

from core import audit
from core.db import get_db
from ui.i18n import columns, t

if not audit.is_admin():          # 메뉴에서 숨기지만, 주소로 직접 들어와도 막는다
    st.error(t("p6.not_admin"))
    st.stop()

db = get_db()
summary = db.query(
    """
    SELECT COUNT(DISTINCT email) AS users,
           SUM(event = 'login') AS logins,
           SUM(event LIKE 'llm:%%') AS llm,
           COUNT(DISTINCT CASE WHEN DATE(at) = CURDATE() THEN email END) AS users_today,
           SUM(event = 'login' AND DATE(at) = CURDATE()) AS logins_today,
           SUM(event LIKE 'llm:%%' AND DATE(at) = CURDATE()) AS llm_today
    FROM access_logs
    """, label="기록: 요약 지표")[0]
with st.container(horizontal=True):
    st.metric(t("p6.users"), int(summary["users"] or 0), t("p6.today", n=int(summary["users_today"] or 0)),
              delta_color="off", border=True)
    st.metric(t("p6.logins"), int(summary["logins"] or 0), t("p6.today", n=int(summary["logins_today"] or 0)),
              delta_color="off", border=True)
    st.metric(t("p6.llm"), int(summary["llm"] or 0), t("p6.today", n=int(summary["llm_today"] or 0)),
              delta_color="off", border=True)

kinds = [r["event"] for r in db.query("SELECT DISTINCT event FROM access_logs ORDER BY event",
                                      label="기록: 이벤트 조회")]
picked = st.pills(t("p6.filter"), kinds, selection_mode="multi", default=kinds)

where, params = "", ()
if picked:
    where = f"WHERE event IN ({', '.join(['%s'] * len(picked))})"
    params = tuple(picked)
events = db.query(
    f"""SELECT at, event, email, name, detail FROM access_logs {where}
        ORDER BY at DESC LIMIT 500""", params, label="기록: 이벤트 조회") if picked else []

st.markdown(t("p6.recent"))
st.dataframe(columns(events), hide_index=True, alt=t("p6.recent_alt"))
if events:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(events[0]))
    writer.writeheader()
    writer.writerows(events)
    st.download_button(t("p6.download"), buf.getvalue().encode("utf-8-sig"), file_name="access_logs.csv",
                       mime="text/csv", icon=":material/download:")

per_user = db.query(
    """
    SELECT email, MAX(name) AS name, MAX(at) AS last_seen,
           SUM(event = 'login') AS logins, SUM(event LIKE 'llm:%%') AS llm_actions
    FROM access_logs WHERE email IS NOT NULL
    GROUP BY email ORDER BY last_seen DESC
    """, label="기록: 사용자별 집계")
st.markdown(t("p6.per_user"))
st.dataframe(columns(per_user), hide_index=True, alt=t("p6.per_user_alt"))
st.caption(t("p6.caption"))

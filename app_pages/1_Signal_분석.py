"""① Signal 분석: 원문 목록 → AI 분석(analysis_graph, 원문마다 병렬) → 구조화된 투자 기회."""
import time

import streamlit as st

from core import analyzer, workflow
from core.audit import log_event
from core.db import get_db
from ui.common import localized
from ui.i18n import columns, t

db = get_db()
signals = db.query(
    "SELECT id, source, author, posted_at, content, analyzed FROM raw_signals ORDER BY id",
    label="화면: 원문 목록")
todo = [s for s in signals if not s["analyzed"]]

st.write(t("p1.intro"))

with st.container(horizontal=True, vertical_alignment="bottom"):
    st.metric(t("p1.metric_signals"), t("p1.count", n=len(signals)), border=True)
    st.metric(t("p1.metric_todo"), t("p1.count", n=len(todo)), border=True)
    run = st.button(t("p1.run", n=len(todo)), type="primary", disabled=not todo, icon=":material/play_arrow:")
    if st.button(t("p1.rerun"), disabled=bool(todo), icon=":material/refresh:"):
        analyzer.reset_analysis(db)
        st.session_state.pop("analysis_result", None)
        st.rerun()

if run:
    log_event("llm:analysis", {"signals": len(todo)})
    progress = st.progress(0.0, text=t("p1.progress_start"))
    done, skipped, start = 0, [], time.perf_counter()
    for update in workflow.get_analysis_graph().stream(
            {}, {"max_concurrency": workflow.ANALYSIS_CONCURRENCY}, stream_mode="updates"):
        for node, value in update.items():
            if node == "analyze_signal":
                done += 1
                progress.progress(done / len(todo), text=t("p1.progress", done=done, total=len(todo),
                                                           c=workflow.ANALYSIS_CONCURRENCY))
            if value and value.get("skipped"):
                skipped += value["skipped"]
            if node == "save_opportunities":
                progress.progress(1.0, text=t("p1.progress_done", n=value["saved"],
                                              s=f"{time.perf_counter() - start:.0f}"))
    st.session_state["analysis_result"] = {"skipped": skipped}
    st.rerun()

opportunities = db.query(
    """
    SELECT o.signal_id AS `signal`, o.title AS opportunity, o.asset, o.chain AS location, p.name AS protocol,
           o.action_type AS type, o.expected_apy AS apy, o.risk_level AS risk, o.confidence, o.summary
    FROM opportunities o JOIN protocols p ON p.id = o.protocol_id
    ORDER BY o.risk_level, o.expected_apy DESC
    """, label="화면: 투자 기회 목록")

tab_opp, tab_raw = st.tabs([t("p1.tab_opps", n=len(opportunities)), t("p1.tab_raw", n=len(signals))])
with tab_opp:
    if opportunities:
        texts = localized([x for o in opportunities for x in (o["opportunity"], o["summary"])])
        rows = [{**o, "opportunity": texts[i * 2], "summary": texts[i * 2 + 1], "type": t("action." + o["type"])}
                for i, o in enumerate(opportunities)]
        st.dataframe(columns(rows), hide_index=True, alt=t("p1.tab_opps", n=len(rows)))
        st.caption(t("p1.embed_caption"))
    else:
        st.info(t("p1.empty"))
    if result := st.session_state.get("analysis_result"):
        with st.expander(t("p1.skipped", n=len(result["skipped"]))):
            reasons = localized([s["reason"] for s in result["skipped"]])
            st.dataframe(columns([{"signal": s["signal_id"], "reason": r} for s, r in zip(result["skipped"], reasons)]),
                         hide_index=True, alt=t("p1.skipped", n=len(reasons)))
with tab_raw:
    st.caption(t("p1.raw_caption"))
    st.dataframe(
        columns([{"id": s["id"], "source": s["source"], "author": s["author"], "posted_at": s["posted_at"],
                  "analyzed": "✓" if s["analyzed"] else "", "content": s["content"]} for s in signals]),
        hide_index=True, alt=t("p1.tab_raw", n=len(signals)))

"""记忆链路里「真正经过 celery worker」那一段的冒烟。

跑之前需要两步准备（都在仓库根目录）：

  1) 一份把记忆打开的临时配置（config/*.local.yaml 已被 gitignore）：
     python -c "import yaml; d=yaml.safe_load(open('config/config.yaml',encoding='utf-8')); d['memory'].update({'enable':True,'enable_summary':True,'enable_profile':True,'enable_vector':True}); open('config/smoke.local.yaml','w',encoding='utf-8').write(yaml.safe_dump(d,allow_unicode=True,sort_keys=False))"
     ※ 必须改文件而不是用环境变量：YAML 里写了 key 就以 YAML 为准，`RAG_MEMORY_*` 顶不动它。

  2) 一个只吃独立队列的 worker（不会抢生产 worker 的活，也别改生产配置）：
     RAG_CONFIG=D:/code/myrag/config/smoke.local.yaml celery -A celery_app worker -Q smoke_queue -n smokeworker@%h --pool=solo --loglevel=INFO
     （PowerShell：$env:RAG_CONFIG="D:/code/myrag/config/smoke.local.yaml"; 然后同一条命令）

  3) 跑本脚本（同样要 RAG_CONFIG 指到那份配置，否则“读”这一侧的开关是关的）：
     RAG_CONFIG=D:/code/myrag/config/smoke.local.yaml python src/memory_worker_smoke.py

覆盖：worker 真的执行了抽取 → PG + Qdrant 双写→ 游标/租约写进 chat_session.meta →
      紧接的第二次被租约跳过 → 开关关着的 worker 直接 skip → 测试数据自动清干净。
测完关掉临时 worker（Windows）：
  powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*smokeworker*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from celery.result import AsyncResult
from qdrant_client import models

import memory_service as ms
from celery_app import app
from chat_store import chat_store
from pg_store import pg_store
from qdrant_store import qdrant_store
from settings import settings
from tasks import memory_update_task

U = "zz_smoke_worker_user"
fails = []
def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if extra == "" else f" | {extra}"))
    if not cond:
        fails.append(name)

# 前置清理（上一次跑残留）
ms.purge_user(U)
for s in chat_store.list_sessions(user_id=U):
    chat_store.delete_session(s["chat_id"])

cid = chat_store.create_session(user_id=U, title="__smoke_worker__")
for role, text in [
    ("user", "以后所有回答都用中文，并且先给结论再展开"),
    ("assistant", "好的。"),
    ("user", "我在准备一份关于成本中心拆分的材料，只看 2025 年的数据"),
    ("assistant", "收到，只按 2025 年口径。"),
]:
    chat_store.add_msg(cid, role, text)
print(f"[会话] chat_id={cid} 已写入 4 条消息")

print("[投递] → 队列 smoke_queue（我的临时 worker）")
r1 = memory_update_task.apply_async(kwargs={"user_id": U, "chat_id": cid}, queue="smoke_queue")
print(f"[投递] task_id={r1.id} state={r1.state}")
stats1 = r1.get(timeout=240)
print("[结果1]", stats1)
chk("W1 worker 真的执行了抽取（返回 profile 统计）", isinstance(stats1, dict) and "profile" in stats1, stats1)

rows = pg_store.query_all(
    "SELECT kind, content, weight, hits, status, source_chat_id, meta FROM rag_memory WHERE user_id=%s AND status='active'",
    (U,),
)
print("[PG 学到的记忆]", [(r["kind"], r["content"]) for r in rows])
chk("W2 记忆已落 PG（worker 写的，不是本地进程）", len(rows) >= 1, len(rows))
chk("W3 条目带 source_chat_id（可追溯来源会话）", all(r["source_chat_id"] == cid for r in rows))
meta = ms._read_meta(cid)
chk("W4 抽取游标写在 chat_session.meta", bool(meta.get("memory_extract_cursor")), meta.get("memory_extract_cursor"))
chk("W5 租约时间戳写在 meta（并发保护生效）", bool(meta.get("memory_lease_at")), meta.get("memory_lease_at"))

ids = [r["memory_id"] for r in pg_store.query_all("SELECT memory_id FROM rag_memory WHERE user_id=%s", (U,))]
pts, _ = qdrant_store.client.scroll(
    collection_name=settings.memory.qdrant_collection_name,
    scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=U))]),
    limit=20, with_payload=True,
)
chk("W6 Qdrant 里也有对应向量（条数一致）", len(pts) == len(ids), f"PG={len(ids)} Qdrant={len(pts)}")

# 再问一句：记忆应当被注入到回答里（走 rag_chat_qa，但把 celery 投递指到 smoke_queue 由本地覆盖不行，
# 所以这里只验证「读」这条路径；写路径上面已经验过）
ctx = ms.load_for_prompt(U, cid, "成本中心拆分怎么做起")
chk("W7 换一个提问也能读到刚学到的记忆", bool(ctx.profile_lines or ctx.recall_lines), ctx.profile_lines + ctx.recall_lines)

# 租约：跑完即释放 ⇒ 紧接着再投一次会正常执行（有新消息才干活，没新消息游标兜住）
chk("W8a 第一次跑完后租约已释放", int(ms._read_meta(cid).get("memory_lease_at", 0)) == 0,
    ms._read_meta(cid).get("memory_lease_at"))
# 真并发防护：手工占一个租约，worker 必须跳过
pg_store.execute(
    "UPDATE chat_session SET meta = jsonb_set(COALESCE(meta,'{}'::jsonb),'{memory_lease_at}',to_jsonb(%s::bigint)) WHERE chat_id=%s",
    (int(time.time()), cid))
r2 = memory_update_task.apply_async(kwargs={"user_id": U, "chat_id": cid}, queue="smoke_queue")
stats2 = r2.get(timeout=120)
chk("W8b 租约被占着 ⇒ worker 跳过（真并发防重复烧 LLM）", str(stats2.get("skipped", "")).startswith("another"), stats2)
pg_store.execute(
    "UPDATE chat_session SET meta = jsonb_set(COALESCE(meta,'{}'::jsonb),'{memory_lease_at}','0'::jsonb) WHERE chat_id=%s",
    (cid,))

# 你的生产 worker（config 里 memory.enable=false）应当直接跳过：证明总开关在 worker 侧也管用
r3 = memory_update_task.apply_async(kwargs={"user_id": U, "chat_id": cid})
stats3 = r3.get(timeout=180)
chk("W9 默认队列上的 worker（enable=false）⇒ 直接 skip", stats3 == {"skipped": "memory off"}, stats3)

# 清理
print("[清理]", ms.purge_user(U), "条记忆已硬删")
chat_store.delete_session(cid)
pts, _ = qdrant_store.client.scroll(
    collection_name=settings.memory.qdrant_collection_name,
    scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=U))]),
    limit=5, with_payload=False,
)
chk("W10 测试数据已全部清干净",
    pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (U,))["c"] == 0
    and len(pts) == 0 and chat_store.get_session(cid) is None, f"残留向量={len(pts)}")

print("\n==== worker 链路结果 ====")
print("全部通过 ✅" if not fails else f"失败项 {len(fails)}: {fails}")

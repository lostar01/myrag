"""记忆功能端到端冒烟（连真实 PG / Qdrant / embedding / ollama / HTTP 接口层）。

跑法（任意目录都行）：
    python src/memory_smoke.py

会创建一次性用户（zz_smoke_*）与一次性会话，测完全部清掉；不修改配置文件：
`memory.enable` 在 config.yaml 里保持 false，开关只在本进程内打开。

覆盖：写入去重 / 语义召回与用户隔离 / Tier A+B 注入 / 抽取与游标 / 并发租约 / 滞动摘要 /
      用户自助管理与向量同步 / “用户删过的不会被自动加回” / 总开关关掉后的行为 /
      `/memories` 接口（鉴权、参数校验、跨用户隐私）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qdrant_client import models

import memory_service as ms
from chat_store import chat_store
from pg_store import pg_store
from qdrant_store import qdrant_store
from rag_prompt import assemble_prompt, build_memory_block
from settings import settings

U = "zz_smoke_memory_user"
OTHER = "zz_smoke_other_user"

# 只在本进程内打开记忆（不写配置文件）
settings.memory.enable = True
settings.memory.enable_summary = True
settings.memory.enable_profile = True
settings.memory.enable_vector = True
settings.memory.recall_min_score = 0.30  # 冒烟期放宽，方便看到真实分数分布
settings.memory.summary_trigger_ratio = 0.15  # 冒烟期调低触发线（真实默认 0.6），否则要堆很多轮才压

print(f"[冒烟开关] 仅本进程生效：enable={ms.cfg.enable} "
      f"summary={ms.cfg.enable_summary} profile={ms.cfg.enable_profile} "
      f"vector={ms.cfg.enable_vector} recall_min_score={ms.cfg.recall_min_score} "
      f"trigger_ratio={ms.cfg.summary_trigger_ratio}")

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if extra == "" else f" | {extra}"))
    if not cond:
        fails.append(name)


def hard_clean():
    rows = pg_store.query_all("SELECT memory_id FROM rag_memory WHERE user_id = %s", (U,))
    ids = [r["memory_id"] for r in rows]
    if ids:
        ms.memory_vectors.delete(ids)
    pg_store.execute("DELETE FROM rag_memory WHERE user_id = %s", (U,))
    # 上一轮跑崩了会残留会话 ⇒ 开头一并清掉，保证冒烟可重复跑
    for s in chat_store.list_sessions(user_id=U):
        chat_store.delete_session(s["chat_id"])


hard_clean()
chat_id = chat_store.create_session(user_id=U, title="__smoke_memory__")
print(f"[冒烟会话] chat_id={chat_id}")

# ---------- A：写入与去重（PG 真相）----------
r1 = ms._upsert_item(U, "preference", "回答要简短，结论写在前面", 0.9, chat_id)
chk("A1 首次写入拿到 memory_id", bool(r1 and r1["memory_id"]), (r1 or {}).get("memory_id"))
r2 = ms._upsert_item(U, "preference", "  回答要简短，\n结论写在前面  ", 0.5, chat_id)
chk("A2 换空白写法 ⇒ 判为重复且同一 id", bool(r2["duplicated"] and r2["memory_id"] == r1["memory_id"]))
row = pg_store.query_one(
    "SELECT hits, weight, expires_at, status, meta FROM rag_memory WHERE memory_id=%s", (r1["memory_id"],)
)
chk("A3 重复只累加 hits，不新增条目", row["hits"] == 2, f"hits={row['hits']}")
chk("A4 weight 取较大值（0.9 不被 0.5 顶掉）", abs(float(row["weight"]) - 0.9) < 1e-6, row["weight"])
chk("A5 自动写 expires_at（TTL 防污染）", row["expires_at"] is not None, row["expires_at"])
chk("A6 meta 记了来源", "source" in (row["meta"] or {}), row["meta"])
cnt = pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (U,))["c"]
chk("A7 同一用户仍只有 1 条", cnt == 1, cnt)

# ---------- B：语义召回 + 用户隔离 ----------
ms._upsert_item(U, "profile", "用户是做财务的，关心成本中心和报销", 0.8, chat_id)
ms._upsert_item(U, "preference", "只看 2024 年之后的资料", 0.7, chat_id)
rec = ms._recall(U, "2024 年的成本中心费用应该怎么归类", ())
chk("B1 能语义召回到相关记忆", len(rec) > 0, [(r["content"], round(r["score"], 3)) for r in rec])
chk("B2 换一个用户什么都召回不到（隐私）", ms._recall(OTHER, "2024 年的成本中心费用应该怎么归类", ()) == [])
scores = [round(r["score"], 3) for r in ms._recall(U, "随便问点完全不相关的东西： Mars 探测车", ())]
print(f"[B3 无关问题的召回分数（用来定 recall_min_score）] {scores}")

# ---------- C：注入问答上下文（Tier A + Tier B + 命中统计）----------
ctx = ms.load_for_prompt(U, chat_id, "本月费用报销规则是什么")
chk("C1 Tier A 固定画像已注入", len(ctx.profile_lines) > 0, ctx.profile_lines)
chk("C2 Tier B 不与 Tier A 重复", not (set(ctx.profile_ids) & set(ctx.recall_ids)),
    f"tierB={ctx.recall_lines} scores={ctx.recall_scores}")
hit = pg_store.query_one("SELECT hits FROM rag_memory WHERE memory_id=%s", (ctx.profile_ids[0],))
chk("C3 注入后 hits 上涨", int(hit["hits"]) >= 2, hit["hits"])
chk("C4 没有 user_id ⇒ 完全不带记忆", ms.load_for_prompt(None, chat_id, "问题").is_empty())
blk = build_memory_block(ctx.profile_lines, ctx.recall_lines, ctx.summary)
p = assemble_prompt("user: 你好\n", "【章节路径】1\n正文内容", "本月费用报销规则是什么", blk)
chk("C5 记忆段落插在「历史对话」之前", blk and p.index("## 用户画像与偏好") < p.index("## 历史对话"))
chk("C6 画像段落带了「不得当知识来源」的提示", "不得当作知识库事实来源" in p)
chk("C7 空记忆 ⇒ 段落完全不出现", build_memory_block([], [], "") == "" and "## 用户画像" not in assemble_prompt("h", "c", "q"))

# ---------- D：画像抽取（真 LLM）----------
turns = [
    ("user", "以后回答都简短点，别长篇大论"),
    ("assistant", "好的，我尽量简短。"),
    ("user", "我是公司财务，主要管成本中心和费用报销"),
    ("assistant", "明白，需要我按成本中心维度来分析吗？"),
    ("user", "对，而且只看 2024 年之后的数据，之前的没用"),
    ("assistant", "收到。"),
]
for role, text in turns:
    chat_store.add_msg(chat_id, role, text)

# 用户手写一条：自动抽取不能把它归档（上一轮冒烟就发现模型会“觉得重复”就删）
manual = ms.create_manual(U, "preference", "称呼用户为“张工”")
chk("D0 手动条目已写入（meta.source=manual）", bool(manual), (manual or {}).get("memory_id"))

stats = ms.run_update(U, chat_id)
print("[D1 run_update 统计]", stats)
chk("D1b 首次 run_update 真的跑到了抽取（租约能拿到）", "profile" in stats, stats)
prof = ms._load_profile_rows(U, 40)
chk("D2 抽取跑完后有条目（且没堆重复）", len(prof) >= 3, [p_["content"] for p_ in prof])
chk("D2b 手动条目不会被自动抽取归档", any(p_["memory_id"] == manual["memory_id"] for p_ in prof))
meta = ms._read_meta(chat_id)
chk("D3 抽取游标已写入 meta", bool(meta.get("memory_extract_cursor")), meta.get("memory_extract_cursor"))
# 租约：跑完即释放（否则用户连续提问会被 skip）⇒ 正常紧接第二次不会跳过；
# 真并发靠“租约被别人占着”来防，这里手工占一个未来租约来验证。
chk("D4a 跑完后租约已释放", int(ms._read_meta(chat_id).get("memory_lease_at", 0)) == 0,
    ms._read_meta(chat_id).get("memory_lease_at"))
pg_store.execute(
    "UPDATE chat_session SET meta = jsonb_set(COALESCE(meta,'{}'::jsonb),'{memory_lease_at}',to_jsonb(%s::bigint)) WHERE chat_id=%s",
    (int(__import__("time").time()), chat_id))
held = ms.run_update(U, chat_id)
chk("D4b 租约被别人占着 ⇒ skip（不重复烧 LLM）", str(held.get("skipped", "")).startswith("another"), held)
pg_store.execute(
    "UPDATE chat_session SET meta = jsonb_set(COALESCE(meta,'{}'::jsonb),'{memory_lease_at}','0'::jsonb) WHERE chat_id=%s",
    (chat_id,))

# 无依据的 remove / 推断出的偏好必须被拦（实测 qwen2.5:7b 会犯）
ok_lex = ms._remove_supported("回答要简短", "user: 以后回答都简短点")
bad_lex = ms._remove_supported("用户是做财务的", "user: 广东的呢？")
chk("D4c remove 依据校验：相关放行 / 无关拦下", ok_lex and not bad_lex, f"{ok_lex}/{bad_lex}")
chk("D4d 一次性提问不推断成年度偏好",
    not ms._add_supported("只看2024年之后的资料", "user: 2024年的收入是多少")
    and ms._add_supported("只看2024年之后的资料", "user: 以后只看2024年之后的资料"))
direct = ms._extract_profile(U, chat_id)
chk("D5 游标：没有新消息就不再调 LLM", direct.get("seen") == 0, direct)
before_cnt = pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (U,))["c"]
ms._upsert_item(U, "preference", "回答要简短，结论写在前面", 0.9, chat_id)  # 语义完全相同
after_cnt = pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (U,))["c"]
chk("D6 模型重复吐同一条 ⇒ 条目数不变", before_cnt == after_cnt, f"{before_cnt} -> {after_cnt}")

# 用户手动删过的条目，自动抽取不能再把它“复活”（上一轮冒烟发现会出现孤儿向量）
ms.delete_memory(U, r1["memory_id"])
re_up = ms._upsert_item(U, "preference", "回答要简短，结论写在前面", 0.9, chat_id)
chk("D7 用户删过 ⇒ 重写不复活（返回 user_deleted）", bool(re_up.get("user_deleted")) and re_up["duplicated"], re_up)
chk("D7b 条目仍是 archived",
    pg_store.query_one("SELECT status FROM rag_memory WHERE memory_id=%s", (r1["memory_id"],))["status"] == "archived")
chk("D7c 它的向量也不存在（无孤儿向量）",
    r1["memory_id"] not in {x["memory_id"] for x in ms._recall(U, "回答要简短结论在前", ())})

# ---------- E：滚动摘要（真 LLM）----------
long_line = "费用报销需要三级审批，并附发票原件、复印件和合同编号，超预算要先走总经理审批"
for i in range(14):
    chat_store.add_msg(chat_id, "user", f"补充规则{i}：" + long_line * 6)
    chat_store.add_msg(chat_id, "assistant", f"已记录规则{i}：三级审批 + 发票原件/复印件 + 合同编号 + 超预算走总经理。")
s = ms._update_summary(chat_id)
print("[E1 摘要统计]", s)
meta = ms._read_meta(chat_id)
chk("E2 摘要已生成并存进 chat_session.meta", bool(meta.get("summary")), (meta.get("summary") or "")[:80].replace("\n", " / "))
chk("E3 摘要覆盖游标已写入", bool(meta.get("summary_cover_at")), meta.get("summary_cover_at"))
chk("E4 已覆盖过的不会再重复压缩", ms._update_summary(chat_id) is None)
chk("E5 下一轮问答能读到摘要", ms.load_for_prompt(U, chat_id, "报销要走几级审批").summary != "")

# ---------- F：用户自助管理（列表/改/删/清空 + 向量同步）----------
items, total = ms.list_for_user(U)
chk("F1 列表 total 与条数一致", total == len(items) and total >= 3, total)
upd = ms.update_memory(U, items[0]["memory_id"], content="回答要先给结论，控制在 200 字内")
chk("F2 改内容 ⇒ 归档旧条目 + 建新条目", bool(upd) and upd["memory_id"] != items[0]["memory_id"])
old_status = pg_store.query_one("SELECT status FROM rag_memory WHERE memory_id=%s", (items[0]["memory_id"],))["status"]
chk("F3 旧条目变 archived（软删，保留审计）", old_status == "archived", old_status)
chk("F4 别的用户改不动（无管理员旁路式越权）",
    ms.update_memory(OTHER, items[0]["memory_id"], content="越权改别人记忆") is None)
chk("F5 别的用户删不动", ms.delete_memory(OTHER, items[0]["memory_id"]) is False)
ids_recalled = {r["memory_id"] for r in ms._recall(U, "回答要先给结论 控制在 200 字内", ())}
chk("F6 归档条目的向量已删（召回不到旧内容）", items[0]["memory_id"] not in ids_recalled)
chk("F7 删除单条", ms.delete_memory(U, upd["memory_id"]) is True)
cleared = ms.clear_all(U)
chk("F8 一键清空", cleared >= 1, cleared)
chk("F9 清空后没有 active 条目",
    pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s AND status='active'", (U,))["c"] == 0)

pts, _ = qdrant_store.client.scroll(
    collection_name=ms.cfg.qdrant_collection_name,
    scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=U))]),
    limit=10,
    with_payload=False,
)
chk("F10 Qdrant 里该用户的向量也清空了（无孤儿向量）", len(pts) == 0, len(pts))

# ---------- G：总开关关掉后的行为 ----------
settings.memory.enable = False
chk("G1 enable=false ⇒ 什么都不注入", ms.load_for_prompt(U, chat_id, "问题").is_empty())
chk("G2 enable=false ⇒ 抽取任务直接跳过", ms.run_update(U, chat_id) == {"skipped": "memory off"})
chk("G3 enable=false ⇒ 摘要读不到", ms.get_summary(chat_id) == "")
settings.memory.enable = True

# ---------- H：/memories 接口层（鉴权 / 参数校验 / 隐私）----------
try:
    from fastapi.testclient import TestClient

    import main_api
    from user_store import create_token, user_store

    # 不用 with ⇒ 不触发 startup 事件（不去碰管理员引导）
    client = TestClient(main_api.app)
    ua = user_store.create_user(username="zz_smoke_api_a", password="smoke123456")
    ub = user_store.create_user(username="zz_smoke_api_b", password="smoke123456")
    ha = {"Authorization": f"Bearer {create_token(ua['user_id'], ua['username'], ua['role'])}"}
    hb = {"Authorization": f"Bearer {create_token(ub['user_id'], ub['username'], ub['role'])}"}

    chk("H1 无 token ⇒ 401", client.get("/memories").status_code == 401)
    chk("H2 坏 token ⇒ 401", client.get("/memories", headers={"Authorization": "Bearer x.y"}).status_code == 401)
    r = client.get("/memories", headers=ha)
    chk("H3 GET /memories ⇒ 200 且带 memory_enabled 字段", r.status_code == 200 and "memory_enabled" in r.json())
    r = client.post("/memories", headers=ha, json={"kind": "preference", "content": "回答要简短，先给结论"})
    chk("H4 POST 手动加一条 ⇒ 200", r.status_code == 200 and bool(r.json()["memory"]["content"]), r.status_code)
    mid = r.json()["memory"]["memory_id"]
    chk("H5 同内容（只差空白）重复提交 ⇒ 不新增条目",
        client.post("/memories", headers=ha, json={"kind": "preference", "content": " 回答要简短，先给结论 "}).json()["memory"]["memory_id"] == mid)
    chk("H6 kind 非法 ⇒ 422", client.post("/memories", headers=ha, json={"kind": "facts", "content": "一条记忆"}).status_code == 422)
    chk("H7 content 太短 ⇒ 422", client.post("/memories", headers=ha, json={"kind": "preference", "content": "a"}).status_code == 422)
    chk("H8 content 超长 ⇒ 422", client.post("/memories", headers=ha, json={"kind": "preference", "content": "超" * 201}).status_code == 422)
    body = client.get("/memories", headers=ha).json()
    chk("H9 列表能看到刚加的那条", body["total"] == 1 and body["items"][0]["memory_id"] == mid, body["total"])
    chk("H10 另一个用户看不到（隐私）", client.get("/memories", headers=hb).json()["total"] == 0)
    chk("H11 另一个用户删不掉 ⇒ 404", client.delete(f"/memories/{mid}", headers=hb).status_code == 404)
    chk("H12 空 body 的 PUT ⇒ 400", client.put(f"/memories/{mid}", headers=ha, json={}).status_code == 400)
    r = client.put(f"/memories/{mid}", headers=ha, json={"status": "archived"})
    chk("H13 PUT status=archived ⇒ 200", r.status_code == 200 and r.json()["memory"]["status"] == "archived")
    chk("H14 归档后列表里不再出现", client.get("/memories", headers=ha).json()["total"] == 0)
    r = client.put(f"/memories/{mid}", headers=ha, json={"status": "active"})
    chk("H15 PUT status=active ⇒ 重新可用", r.status_code == 200 and client.get("/memories", headers=ha).json()["total"] == 1)
    chk("H16 改内容 ⇒ 返回新 id（旧条目归档）",
        client.put(f"/memories/{mid}", headers=ha, json={"content": "回答控制在 200 字内"}).json()["memory"]["memory_id"] != mid)
    live = client.get("/memories", headers=ha).json()["items"][0]["memory_id"]
    chk("H17 DELETE 单条 ⇒ 200", client.delete(f"/memories/{live}", headers=ha).status_code == 200)
    client.post("/memories", headers=ha, json={"kind": "profile", "content": "用户是做财务的"})
    chk("H18 DELETE /memories 清空 ⇒ 200 且清空后为空",
        client.delete("/memories", headers=ha).status_code == 200 and client.get("/memories", headers=ha).json()["total"] == 0)
    chk("H19 分页参数生效", client.get("/memories?limit=1&offset=0", headers=ha).status_code == 200)
    chk("H20 非法 limit ⇒ 422（Query 校验）", client.get("/memories?limit=0", headers=ha).status_code == 422)

    # 账号删除 ⇒ 记忆彻底清除（走接口：DELETE /users/{id}，需要管理员 token）
    client.post("/memories", headers=ha, json={"kind": "preference", "content": "删账号时该一起消失"})
    ua_before = pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (ua["user_id"],))["c"]
    uc = user_store.create_user(username="zz_smoke_api_admin", password="smoke123456", role="admin")
    hc = {"Authorization": f"Bearer {create_token(uc['user_id'], uc['username'], uc['role'])}"}
    r = client.delete(f"/users/{ua['user_id']}", headers=hc)
    chk("H21 管理员删用户 ⇒ 200", r.status_code == 200, r.json())
    chk("H21b 删用户时记忆一并硬清（PG 无残留）",
        ua_before > 0 and pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (ua["user_id"],))["c"] == 0,
        f"删除前 {ua_before} 条")
    for uid in (ua["user_id"], ub["user_id"]):
        ms.purge_user(uid)  # 幂等：把 H21b 可能残留的向量收尾
        pts, _ = qdrant_store.client.scroll(
            collection_name=ms.cfg.qdrant_collection_name,
            scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=uid))]),
            limit=5,
            with_payload=False,
        )
        chk(f"H22 接口冒烟用户的向量已清空（{uid[:8]}…）", len(pts) == 0, len(pts))
    user_store.delete_user(ub["user_id"])
    user_store.delete_user(uc["user_id"])
    chk("H23 接口冒烟用户已全部删除",
        all(user_store.get_by_id(x["user_id"]) is None for x in (ua, ub, uc)))
except Exception as e:
    chk("H0 接口冒烟可运行（main_api / TestClient 可用）", False, f"{type(e).__name__}: {e}")

# ---------- Z：清理冒烟数据 ----------
hard_clean()
chat_store.delete_session(chat_id)
chk("Z1 PG 里冒烟条目已清空",
    pg_store.query_one("SELECT COUNT(*) AS c FROM rag_memory WHERE user_id=%s", (U,))["c"] == 0)
chk("Z2 冒烟会话已删除", chat_store.get_session(chat_id) is None)
chk("Z3 冒烟用户的会话列表为空", chat_store.list_sessions(user_id=U) == [])
pts, _ = qdrant_store.client.scroll(
    collection_name=ms.cfg.qdrant_collection_name,
    scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=U))]),
    limit=10,
    with_payload=False,
)
chk("Z4 Qdrant 里也没有冒烟残留", len(pts) == 0, len(pts))
info = qdrant_store.client.get_collection(ms.cfg.qdrant_collection_name)
print(f"[记忆向量集] {ms.cfg.qdrant_collection_name} 已存在，points={info.points_count}, vectors={info.config.params.vectors.size}")

print("\n==== 冒烟结果 ====")
print("全部通过 ✅" if not fails else f"失败项 {len(fails)}: {fails}")

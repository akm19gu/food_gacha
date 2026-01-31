import json
import os
import random
import sqlite3
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import streamlit as st

# 本番は環境変数で場所を変えられる（永続ディスクのパスとか）
DB_PATH = Path(os.environ.get("MENUS_DB_PATH", "menus.db"))

SEED_DB_PATH = Path("menus_seed.db")

def bootstrap_db():
    # 本番でDBがまだ無いなら、seed をコピーして初期データを入れる
    if not DB_PATH.exists() and SEED_DB_PATH.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SEED_DB_PATH, DB_PATH)

# 追加を許可するキー（これが合わないと保存できない）
ADD_KEY = os.environ.get("ADD_KEY", "")
# 削除も守りたいなら別キー（任意）
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

GENRES = ["和", "洋", "中", "その他"]
GROUPS = ["主菜", "副菜", "主食", "乳製品", "果物"]


@dataclass
class RoleOption:
    groups: List[str]
    weight: float = 1.0


@dataclass
class MenuItem:
    id: int
    name: str
    genre: str
    role_options: List[RoleOption]


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def ensure_db():
    con = db()
    con.execute("PRAGMA journal_mode = WAL;")
    con.execute("""
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        genre TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS role_options(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        groups_json TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
    );
    """)
    con.commit()
    con.close()


def load_items() -> List[MenuItem]:
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT i.id, i.name, i.genre, ro.groups_json, ro.weight
        FROM items i
        LEFT JOIN role_options ro ON ro.item_id = i.id
        ORDER BY i.id ASC, ro.id ASC
    """)
    rows = cur.fetchall()
    con.close()

    items: Dict[int, MenuItem] = {}
    for item_id, name, genre, groups_json, weight in rows:
        if item_id not in items:
            items[item_id] = MenuItem(id=item_id, name=name, genre=genre, role_options=[])
        if groups_json is not None:
            items[item_id].role_options.append(
                RoleOption(groups=json.loads(groups_json), weight=float(weight))
            )

    return [x for x in items.values() if x.role_options]


def insert_item(name: str, genre: str, role_options: List[RoleOption]) -> None:
    con = db()
    cur = con.cursor()
    cur.execute("INSERT INTO items(name, genre) VALUES(?, ?)", (name, genre))
    item_id = cur.lastrowid
    for opt in role_options:
        cur.execute(
            "INSERT INTO role_options(item_id, groups_json, weight) VALUES(?, ?, ?)",
            (item_id, json.dumps(opt.groups, ensure_ascii=False), float(opt.weight)),
        )
    con.commit()
    con.close()


def delete_item_by_id(item_id: int) -> None:
    con = db()
    con.execute("DELETE FROM items WHERE id = ?", (item_id,))
    con.commit()
    con.close()


def score_selection(
    selection: List[Tuple[MenuItem, RoleOption]],
    preferred_genre: Optional[str],
    target_dish_count: int,
) -> int:
    score = 0
    items = [it for it, _ in selection]

    genres = [x.genre for x in items]
    if genres:
        base = genres[0]
        same = sum(1 for g in genres if g == base)
        if same == len(genres):
            score += 6
        else:
            score += 2 * max(0, same - 1)
            score -= (len(genres) - same)

    if preferred_genre and preferred_genre != "自動":
        hit = sum(1 for x in items if x.genre == preferred_genre)
        score += 2 * hit

    score -= max(0, len(items) - target_dish_count)
    return score


def generate_menu(
    items: List[MenuItem],
    preferred_genre: Optional[str],
    counts: Dict[str, int],
    tries: int = 450,
) -> Tuple[List[Tuple[MenuItem, RoleOption]], int]:
    # needed を「グループのmultiset（重複あり）」で持つ
    needed: List[str] = []
    for g in GROUPS:
        needed += [g] * max(0, int(counts.get(g, 0)))

    target_dish_count = sum(max(0, int(v)) for v in counts.values())

    best: List[Tuple[MenuItem, RoleOption]] = []
    best_score = -10**9

    for _ in range(tries):
        remaining = needed[:]
        chosen_ids = set()
        selection: List[Tuple[MenuItem, RoleOption]] = []

        for _step in range(max(10, len(remaining) * 3)):
            if not remaining:
                break

            target = random.choice(remaining)

            cands: List[Tuple[MenuItem, RoleOption, float]] = []
            for it in items:
                if it.id in chosen_ids:
                    continue

                genre_bonus = 1.0
                if preferred_genre and preferred_genre != "自動":
                    genre_bonus = 1.25 if it.genre == preferred_genre else 0.9

                for opt in it.role_options:
                    if target in opt.groups:
                        cover = sum(1 for g in opt.groups if g in remaining)
                        w = opt.weight * genre_bonus * (1.0 + 0.6 * max(0, cover - 1))
                        cands.append((it, opt, w))

            if not cands:
                selection = []
                break

            it, opt, _w = random.choices(cands, weights=[w for _, _, w in cands], k=1)[0]
            chosen_ids.add(it.id)
            selection.append((it, opt))

            for g in opt.groups:
                if g in remaining:
                    remaining.remove(g)

        if not selection:
            continue
        if remaining:
            continue

        s = score_selection(selection, preferred_genre, target_dish_count)
        if s > best_score:
            best_score = s
            best = selection

    return best, best_score


# ---- UI ----
bootstrap_db()
ensure_db()
st.set_page_config(page_title="献立ガチャ", page_icon="🍚")
st.title("🍚 献立ガチャ")

# 追加キー入力（ADD_KEY が未設定ならローカル用に無制限）
is_protected_add = bool(ADD_KEY)

if is_protected_add:
    add_key_input = st.text_input("追加キー（知ってる人だけ追加できる）", type="password")
    can_add = (add_key_input == ADD_KEY)
    if not can_add and add_key_input:
        st.warning("追加キーが違うニャ")
else:
    can_add = True
    st.caption("※ ADD_KEY が未設定だから、いまは誰でも追加できる状態ニャ（リリース時は設定推奨）")

if "role_opts" not in st.session_state:
    st.session_state.role_opts = []

with st.expander("メニューを追加", expanded=True):
    c1, c2 = st.columns(2)
    name = c1.text_input("料理名", placeholder="例：チャーハン")
    genre = c2.selectbox("ジャンル", GENRES, index=0)

    st.write("役割パターンを追加して。1品が複数グループを兼ねてもOK。")
    cc1, cc2 = st.columns(2)
    gsel = cc1.multiselect("このパターンのグループ", GROUPS, default=[])
    w = cc2.number_input("このパターンの出やすさ（重み）", min_value=0.1, value=1.0, step=0.1)

    if st.button("この役割パターンを追加"):
        if gsel:
            st.session_state.role_opts.append(RoleOption(groups=gsel, weight=float(w)))
        else:
            st.warning("グループを1つは選んでニャ")

    if st.session_state.role_opts:
        st.write("いまの役割パターン")
        for i, opt in enumerate(st.session_state.role_opts):
            st.write(f"・{i+1}: {' / '.join(opt.groups)}  重み={opt.weight}")
        if st.button("役割パターンを全部クリア"):
            st.session_state.role_opts = []

    save_disabled = not can_add
    if st.button("このメニューを保存", disabled=save_disabled):
        if not name.strip():
            st.warning("料理名が空っぽ")
        elif not st.session_state.role_opts:
            st.warning("役割パターンがないと引けない")
        else:
            try:
                insert_item(name.strip(), genre, st.session_state.role_opts)
                st.session_state.role_opts = []
                st.success("追加しました")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("同じ名前がもうあるみたい。別名にして（ごめんね）")

    if save_disabled:
        st.caption("追加キーが合ってないと保存できないニャ")

st.divider()

items = load_items()
st.subheader("登録済みメニュー")
if not items:
    st.info("まずは ごはん(主食/和), 味噌汁(副菜/和), 生姜焼き(主菜/和) あたりを入れてみよう")
else:
    rows = []
    for it in items:
        patterns = [f"{'・'.join(opt.groups)}(w={opt.weight})" for opt in it.role_options]
        rows.append({"id": it.id, "料理名": it.name, "ジャンル": it.genre, "役割パターン": " / ".join(patterns)})
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("メニューを削除（管理）", expanded=False):
        if not ADMIN_KEY:
            st.caption("ADMIN_KEY が未設定だから削除はロック中ニャ")
        else:
            admin_key_input = st.text_input("管理キー", type="password")
            if admin_key_input != ADMIN_KEY:
                if admin_key_input:
                    st.warning("管理キーが違うニャ")
                st.stop()

            options = {f"{it.id}: {it.name}": it.id for it in items}
            key = st.selectbox("消す料理を選ぶ", list(options.keys()))
            confirm = st.checkbox("本当に削除する")
            if st.button("削除する"):
                if not confirm:
                    st.warning("確認にチェックを入れてください")
                else:
                    delete_item_by_id(options[key])
                    st.success("消しました")
                    st.rerun()

st.divider()
st.subheader("今日の献立を引く")

preferred = st.selectbox("ジャンルの気分", ["自動"] + GENRES, index=0)

st.write("品数（基本は全部1。0にするとその枠は無し）")
cA, cB, cC, cD, cE = st.columns(5)
n_shushoku = cA.selectbox("主食", [0, 1, 2, 3], index=1)
n_shusai = cB.selectbox("主菜", [0, 1, 2, 3], index=1)
n_fukusai = cC.selectbox("副菜", [0, 1, 2, 3], index=1)
n_milk = cD.selectbox("乳製品", [0, 1, 2, 3], index=0)
n_fruit = cE.selectbox("果物", [0, 1, 2, 3], index=0)

counts = {
    "主食": int(n_shushoku),
    "主菜": int(n_shusai),
    "副菜": int(n_fukusai),
    "乳製品": int(n_milk),
    "果物": int(n_fruit),
}

if st.button("ガチャ！"):
    selection, score = generate_menu(items, preferred, counts)
    if not selection:
        st.error("その品数を満たせるだけの候補が足りない。品数を減らすか、登録を増やして")
    else:
        st.markdown("**今日の献立**")
        for it, opt in selection:
            st.write(f"・{it.name}（{it.genre} / 役割: {'・'.join(opt.groups)}）")
        st.caption(f"スコア: {score}")

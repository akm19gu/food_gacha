import json
import os
import random
import sqlite3
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import streamlit as st

# -----------------------------
# 設定
# -----------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()  # Neon等(Postgres)
USE_POSTGRES = bool(DATABASE_URL)

# sqlite fallback（ローカルや保険用）
DB_PATH = Path(os.environ.get("MENUS_DB_PATH", "menus.db"))
SEED_DB_PATH = Path("menus_seed.db")

# 追加を許可するキー（合わないと保存できない）
ADD_KEY = os.environ.get("ADD_KEY", "")
# 削除・編集を守りたいなら管理キー（任意）
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

GENRES = ["和", "洋", "中", "その他"]
GROUPS = ["主菜", "副菜", "主食", "乳製品", "果物"]

DIFFICULTY_LABELS = {
    1: "冷食・レンチン",
    2: "かなり簡単",
    3: "ふつう",
    4: "手間あり",
    5: "コース料理",
}


# -----------------------------
# 型
# -----------------------------
@dataclass
class RoleOption:
    groups: List[str]
    weight: float = 1.0


@dataclass
class MenuItem:
    id: int
    name: str
    genre: str
    difficulty: int
    role_options: List[RoleOption]


# -----------------------------
# DB ユーティリティ
# -----------------------------
def bootstrap_db_sqlite():
    # sqlite運用時：本番でDBがまだ無いならseedをコピー
    if not USE_POSTGRES:
        if (not DB_PATH.exists()) and SEED_DB_PATH.exists():
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SEED_DB_PATH, DB_PATH)


def db_sqlite() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def db_postgres():
    # psycopg3
    import psycopg

    return psycopg.connect(DATABASE_URL)


def db():
    return db_postgres() if USE_POSTGRES else db_sqlite()


def ensure_db():
    con = db()
    cur = con.cursor()

    if USE_POSTGRES:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items(
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                genre TEXT NOT NULL,
                difficulty SMALLINT NOT NULL DEFAULT 3,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS role_options(
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                groups_json TEXT NOT NULL,
                weight DOUBLE PRECISION NOT NULL DEFAULT 1.0
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS role_options_item_id_idx ON role_options(item_id);")

        # 既存DBに後付けでdifficultyが無い場合に備える（冪等）
        cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS difficulty SMALLINT NOT NULL DEFAULT 3;")

    else:
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                genre TEXT NOT NULL,
                difficulty INTEGER NOT NULL DEFAULT 3,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS role_options(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                groups_json TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
            );
            """
        )

        # もし昔のDBでdifficulty列が無い場合に後付け
        cur.execute("PRAGMA table_info(items);")
        cols = {row[1] for row in cur.fetchall()}
        if "difficulty" not in cols:
            cur.execute("ALTER TABLE items ADD COLUMN difficulty INTEGER NOT NULL DEFAULT 3;")

    con.commit()
    con.close()


def load_items() -> List[MenuItem]:
    con = db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            i.id, i.name, i.genre, COALESCE(i.difficulty, 3) as difficulty,
            ro.groups_json, ro.weight
        FROM items i
        LEFT JOIN role_options ro ON ro.item_id = i.id
        ORDER BY i.id ASC, ro.id ASC
        """
    )
    rows = cur.fetchall()
    con.close()

    items: Dict[int, MenuItem] = {}
    for row in rows:
        item_id, name, genre, difficulty, groups_json, weight = row
        if item_id not in items:
            items[item_id] = MenuItem(
                id=int(item_id),
                name=str(name),
                genre=str(genre),
                difficulty=int(difficulty) if difficulty is not None else 3,
                role_options=[],
            )
        if groups_json is not None:
            items[item_id].role_options.append(
                RoleOption(groups=json.loads(groups_json), weight=float(weight))
            )

    return [x for x in items.values() if x.role_options]


def insert_item(name: str, genre: str, difficulty: int, role_options: List[RoleOption]) -> None:
    con = db()
    cur = con.cursor()

    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO items(name, genre, difficulty) VALUES (%s, %s, %s) RETURNING id",
            (name, genre, int(difficulty)),
        )
        item_id = cur.fetchone()[0]
        for opt in role_options:
            cur.execute(
                "INSERT INTO role_options(item_id, groups_json, weight) VALUES (%s, %s, %s)",
                (int(item_id), json.dumps(opt.groups, ensure_ascii=False), float(opt.weight)),
            )
    else:
        cur.execute(
            "INSERT INTO items(name, genre, difficulty) VALUES(?, ?, ?)",
            (name, genre, int(difficulty)),
        )
        item_id = cur.lastrowid
        for opt in role_options:
            cur.execute(
                "INSERT INTO role_options(item_id, groups_json, weight) VALUES(?, ?, ?)",
                (int(item_id), json.dumps(opt.groups, ensure_ascii=False), float(opt.weight)),
            )

    con.commit()
    con.close()


def delete_item_by_id(item_id: int) -> None:
    con = db()
    cur = con.cursor()

    if USE_POSTGRES:
        cur.execute("DELETE FROM items WHERE id = %s", (int(item_id),))
    else:
        cur.execute("DELETE FROM items WHERE id = ?", (int(item_id),))

    con.commit()
    con.close()


def update_item_difficulty(item_id: int, difficulty: int) -> None:
    con = db()
    cur = con.cursor()

    if USE_POSTGRES:
        cur.execute("UPDATE items SET difficulty = %s WHERE id = %s", (int(difficulty), int(item_id)))
    else:
        cur.execute("UPDATE items SET difficulty = ? WHERE id = ?", (int(difficulty), int(item_id)))

    con.commit()
    con.close()


# -----------------------------
# ガチャロジック
# -----------------------------
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
    difficulty_range: Tuple[int, int],
    tries: int = 450,
) -> Tuple[List[Tuple[MenuItem, RoleOption]], int]:
    dmin, dmax = difficulty_range

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
                if not (dmin <= int(it.difficulty) <= dmax):
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


# -----------------------------
# 一覧整形
# -----------------------------
def item_can_cover_group(it: MenuItem, group: str) -> bool:
    return any(group in opt.groups for opt in it.role_options)


def item_any_groups(it: MenuItem) -> List[str]:
    gset = set()
    for opt in it.role_options:
        for g in opt.groups:
            gset.add(g)
    return sorted(gset, key=lambda x: GROUPS.index(x) if x in GROUPS else 999)


def build_rows(items: List[MenuItem]) -> List[Dict[str, str]]:
    rows = []
    for it in items:
        patterns = [f"{'・'.join(opt.groups)}(w={opt.weight})" for opt in it.role_options]
        rows.append(
            {
                "id": it.id,
                "料理名": it.name,
                "ジャンル": it.genre,
                "面倒くささ": f"{it.difficulty}（{DIFFICULTY_LABELS.get(int(it.difficulty), '')}）",
                "役割": "・".join(item_any_groups(it)),
                "役割パターン": " / ".join(patterns),
            }
        )
    return rows


def sort_items(items: List[MenuItem], sort_key: str, asc: bool) -> List[MenuItem]:
    reverse = not asc
    if sort_key == "新しい順":
        return sorted(items, key=lambda x: x.id, reverse=reverse)
    if sort_key == "料理名":
        return sorted(items, key=lambda x: x.name.lower(), reverse=reverse)
    if sort_key == "ジャンル":
        return sorted(items, key=lambda x: GENRES.index(x.genre) if x.genre in GENRES else 999, reverse=reverse)
    if sort_key == "役割の数":
        return sorted(items, key=lambda x: len(item_any_groups(x)), reverse=reverse)
    if sort_key == "面倒くささ":
        return sorted(items, key=lambda x: int(x.difficulty), reverse=reverse)
    return items


# -----------------------------
# UI
# -----------------------------
bootstrap_db_sqlite()
ensure_db()

st.set_page_config(page_title="献立ガチャ", page_icon="🍚")
st.title("🍚 献立ガチャ")

items = load_items()

# 1) ガチャ（最上段）
st.header("🎲 今日の献立を引く")

preferred = st.selectbox("ジャンルの気分", ["自動"] + GENRES, index=0)

diff_min, diff_max = st.slider(
    "面倒くささの気分（範囲）",
    min_value=1,
    max_value=5,
    value=(1, 5),
)

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
    selection, score = generate_menu(items, preferred, counts, (diff_min, diff_max))
    if not selection:
        st.error("その条件を満たせるだけの候補が足りない。品数を減らすか、登録を増やして")
    else:
        st.markdown("**今日の献立**")
        for it, opt in selection:
            st.write(f"・{it.name}（{it.genre} / 面倒くささ:{it.difficulty} / 役割: {'・'.join(opt.groups)}）")
        st.caption(f"スコア: {score}")

st.divider()

# 2) メニュー追加（中段）
st.header("➕ メニューを追加")

if "role_opts" not in st.session_state:
    st.session_state.role_opts = []

with st.expander("入力フォームを開く", expanded=True):
    c1, c2 = st.columns(2)
    name = c1.text_input("料理名", placeholder="例：チャーハン")
    genre = c2.selectbox("ジャンル", GENRES, index=0, key="add_genre")

    st.write("役割パターンを追加して。1品が複数グループを兼ねてもOK。")
    cc1, cc2 = st.columns(2)
    gsel = cc1.multiselect("このパターンのグループ", GROUPS, default=[], key="add_groups")
    w = cc2.number_input("このパターンの出やすさ（重み）", min_value=0.1, value=1.0, step=0.1, key="add_weight")

    if st.button("この役割パターンを追加", key="btn_add_roleopt"):
        if gsel:
            st.session_state.role_opts.append(RoleOption(groups=gsel, weight=float(w)))
        else:
            st.warning("グループを1つは選んでニャ")

    if st.session_state.role_opts:
        st.write("いまの役割パターン")
        for i, opt in enumerate(st.session_state.role_opts):
            st.write(f"・{i+1}: {' / '.join(opt.groups)}  重み={opt.weight}")
        if st.button("役割パターンを全部クリア", key="btn_clear_roleopt"):
            st.session_state.role_opts = []

    difficulty = st.selectbox(
        "面倒くささ（1=冷食〜5=コース料理）",
        [1, 2, 3, 4, 5],
        index=2,
        format_func=lambda x: f"{x}: {DIFFICULTY_LABELS.get(x, '')}",
        key="add_difficulty",
    )

    # --- 追加キーを「保存ボタンの直前」に配置 ---
    can_add = True
    if ADD_KEY:
        add_key_input = st.text_input(
            "追加キー（知ってる人だけ保存できる）",
            type="password",
            key="add_key_input_in_add_form",
        )
        can_add = (add_key_input == ADD_KEY)
        if add_key_input and not can_add:
            st.warning("追加キーが違うニャ")
    else:
        st.caption("※ ADD_KEY 未設定だから、いまは誰でも追加できる状態ニャ（リリース時は設定推奨）")

    save_disabled = not can_add
    if st.button("このメニューを保存", disabled=save_disabled, key="btn_save_item"):
        if not name.strip():
            st.warning("料理名が空っぽ")
        elif not st.session_state.role_opts:
            st.warning("役割パターンがないと引けない")
        else:
            try:
                insert_item(name.strip(), genre, int(difficulty), st.session_state.role_opts)
                st.session_state.role_opts = []
                st.success("追加しました")
                st.rerun()
            except Exception as e:
                # sqlite: IntegrityError / postgres: UniqueViolation などをまとめて扱う
                st.error("同じ名前がもうあるか、DBエラーが出たみたい。別名にしてみて")
                st.caption(str(e)[:200])

    if save_disabled:
        st.caption("追加キーが合ってないと保存できないニャ")

st.divider()

# 3) 登録済みメニュー（下段：絞り込み+ソート、全部表示も可）
st.header("📚 登録済みメニュー")

if not items:
    st.info("まずは ごはん(主食/和), 味噌汁(副菜/和), 生姜焼き(主菜/和) あたりを入れてみよう")
else:
    c1, c2, c3 = st.columns([1.2, 1.2, 1.6])
    view_mode = c1.selectbox("表示モード", ["絞り込み（おすすめ）", "全部表示"], index=0)

    genre_filter = c2.selectbox("ジャンルで絞り込み", ["（指定なし）"] + GENRES, index=0)
    group_filter = c3.selectbox("役割で絞り込み", ["（指定なし）"] + GROUPS, index=0)

    cS1, cS2 = st.columns([1.4, 1.0])
    sort_key = cS1.selectbox("ソート", ["新しい順", "料理名", "ジャンル", "役割の数", "面倒くささ"], index=0)
    asc = (cS2.selectbox("順序", ["降順", "昇順"], index=0) == "昇順")

    filtered = items[:]
    if view_mode != "全部表示":
        if genre_filter != "（指定なし）":
            filtered = [it for it in filtered if it.genre == genre_filter]
        if group_filter != "（指定なし）":
            filtered = [it for it in filtered if item_can_cover_group(it, group_filter)]

    filtered = sort_items(filtered, sort_key, asc)

    st.caption(f"表示件数: {len(filtered)} / 全体: {len(items)}")
    st.dataframe(build_rows(filtered), use_container_width=True, hide_index=True)

    # 管理（難易度編集 & 削除）
    with st.expander("管理（難易度編集・削除）", expanded=False):
        if not ADMIN_KEY:
            st.caption("ADMIN_KEY が未設定だから管理はロック中ニャ")
        else:
            admin_key_input = st.text_input("管理キー", type="password", key="admin_key_input")
            if admin_key_input != ADMIN_KEY:
                if admin_key_input:
                    st.warning("管理キーが違うニャ")
                st.caption("管理キーが合ってると編集・削除できるの")
            else:
                st.subheader("難易度を編集")
                options = {f"{it.id}: {it.name}（いま:{it.difficulty}）": it.id for it in items}
                pick = st.selectbox("対象", list(options.keys()), key="diff_target")
                new_diff = st.selectbox(
                    "新しい面倒くささ",
                    [1, 2, 3, 4, 5],
                    index=2,
                    format_func=lambda x: f"{x}: {DIFFICULTY_LABELS.get(x, '')}",
                    key="diff_value",
                )
                if st.button("難易度を更新", key="btn_update_diff"):
                    update_item_difficulty(options[pick], new_diff)
                    st.success("更新したわ")
                    st.rerun()

                st.subheader("メニューを削除")
                key = st.selectbox("消す料理を選ぶ", list(options.keys()), key="delete_target")
                confirm = st.checkbox("本当に削除する", key="delete_confirm")
                if st.button("削除する", key="btn_delete"):
                    if not confirm:
                        st.warning("確認にチェックを入れてください")
                    else:
                        delete_item_by_id(options[key])
                        st.success("消しました")
                        st.rerun()

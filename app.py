import html
import json
import os
import random
import sqlite3
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set

import streamlit as st
import streamlit.components.v1 as components

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


def request_scroll(anchor_id: str) -> None:
    st.session_state["_scroll_to"] = anchor_id


def run_scroll_if_needed() -> None:
    anchor = st.session_state.get("_scroll_to")
    if not anchor:
        return

    # mobileだけで発火（幅は好みで調整）
    js = f"""
    <script>
    (function() {{
      const isMobile = window.parent.matchMedia("(max-width: 768px)").matches;
      if (!isMobile) return;

      const id = {json.dumps(anchor)};
      let tries = 0;

      function go() {{
        const el = window.parent.document.getElementById(id);
        if (el) {{
          el.scrollIntoView({{ behavior: "smooth", block: "start" }});
        }} else if (tries < 25) {{
          tries++;
          setTimeout(go, 80);
        }}
      }}

      setTimeout(go, 40);
    }})();
    </script>
    """
    components.html(js, height=0)

    # 1回だけでいいから消す
    st.session_state["_scroll_to"] = None


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


def _load_items_from_db() -> List[MenuItem]:
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


@st.cache_data(show_spinner=False)
def load_items_cached(items_ver: int) -> List[MenuItem]:
    # items_verが変わったら自動で無効化される
    return _load_items_from_db()


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


def feasible_auto_base_genres(
    items: List[MenuItem],
    counts: Dict[str, int],
    difficulty_range: Tuple[int, int],
) -> List[str]:
    """自動ジャンルで '基準ジャンル + その他' だけで必要グループを満たせそうな基準ジャンルを返す"""
    dmin, dmax = difficulty_range
    required = [g for g in GROUPS if int(counts.get(g, 0)) > 0]

    bases = [g for g in GENRES if g != "その他"]
    ok_bases: List[str] = []

    for base in bases:
        allowed = {base, "その他"}
        ok = True
        for group in required:
            exists = any(
                (it.genre in allowed)
                and (dmin <= int(it.difficulty) <= dmax)
                and any(group in opt.groups for opt in it.role_options)
                for it in items
            )
            if not exists:
                ok = False
                break
        if ok:
            ok_bases.append(base)

    return ok_bases


# -----------------------------
# ガチャロジック
# -----------------------------
def _genre_cluster(genre: str, preferred_genre: Optional[str]) -> str:
    """
    ジャンルの“同系統”判定。
    和を選んだときは、和＋中を同じ系統として扱って「混ぜても不利すぎない」ようにする。
    """
    if preferred_genre == "和" and genre in ("和", "中"):
        return "和中"
    return genre


def _genre_policy(preferred_genre: Optional[str], base_genre: Optional[str]) -> Tuple[Optional[Set[str]], Dict[str, float]]:
    """
    返り値:
      allowed_genres: Noneならフィルタなし / setならその中だけ許可
      bonus_map: genre -> bonus（weights用）
    ルール:
      - 和: 和＋中（ちょい混ぜ）＋その他
      - 洋: 洋＋その他（和/中は混ぜない）
      - 中: 中＋その他（和/洋は混ぜない）
      - その他: その他のみ
      - 自動: base + その他
    """
    if not preferred_genre:
        return None, {}

    if preferred_genre == "自動":
        if base_genre is None:
            return None, {}
        return {base_genre, "その他"}, {base_genre: 1.18, "その他": 0.96}

    if preferred_genre == "和":
        allowed = {"和", "中", "その他"}
        # 和はしっかり寄せつつ、中も少しだけ通す
        bonus = {"和": 1.32, "中": 0.70, "その他": 0.30}
        return allowed, bonus

    if preferred_genre == "洋":
        allowed = {"洋", "その他"}
        bonus = {"洋": 1.28, "その他": 0.30}
        return allowed, bonus

    if preferred_genre == "中":
        allowed = {"中", "その他"}
        bonus = {"中": 1.28, "その他": 0.30}
        return allowed, bonus

    if preferred_genre == "その他":
        return {"その他"}, {"その他": 1.0}

    return None, {}


def score_selection(
    selection: List[Tuple[MenuItem, RoleOption]],
    preferred_genre: Optional[str],
    target_dish_count: int,
) -> int:
    score = 0
    items2 = [it for it, _ in selection]

    genres = [x.genre for x in items2]
    if genres:
        # 和を選んだときは、和＋中を同一クラスタとして扱う
        clustered = [_genre_cluster(g, preferred_genre) for g in genres]
        base = clustered[0]
        same = sum(1 for g in clustered if g == base)
        if same == len(clustered):
            score += 6
        else:
            score += 2 * max(0, same - 1)
            score -= (len(clustered) - same)

    # ジャンル指定の加点（和だけ中華にも少し点を渡す）
    if preferred_genre and preferred_genre != "自動":
        if preferred_genre == "和":
            wa = sum(1 for x in items2 if x.genre == "和")
            chu = sum(1 for x in items2 if x.genre == "中")
            score += 2 * wa + 1 * chu
        else:
            hit = sum(1 for x in items2 if x.genre == preferred_genre)
            score += 2 * hit

    score -= max(0, len(items2) - target_dish_count)
    return score


def _selection_signature_and_ids(selection: List[Tuple[MenuItem, RoleOption]]) -> Tuple[str, List[int]]:
    ids = sorted({int(it.id) for it, _ in selection})
    sig = "-".join(str(x) for x in ids)
    return sig, ids


def resolve_difficulty_preset(preset: Optional[str]) -> Tuple[Tuple[int, int], str]:
    """
    preset:
      None        -> 自動
      microwave   -> レンチンばんざい
      usual       -> いつものごはん
      deluxe      -> ごうかなディナー
      chef        -> シェフのおまかせコース

    return:
      difficulty_range, pick_mode
    """
    if preset == "microwave":
        return (1, 1), "microwave"
    if preset == "usual":
        return (2, 3), "usual"
    if preset == "deluxe":
        return (2, 4), "deluxe"
    if preset == "chef":
        return (2, 5), "chef"
    return (1, 5), "auto"


def generate_candidates(
    items: List[MenuItem],
    preferred_genre: Optional[str],
    counts: Dict[str, int],
    difficulty_range: Tuple[int, int],
    base_genre: Optional[str] = None,   # 自動ジャンル時の基準ジャンル
    tries: int = 650,
    keep: int = 260,
) -> List[Tuple[List[Tuple[MenuItem, RoleOption]], int, str, List[int]]]:
    """
    候補をたくさん作って返す（スコア付き）。
    返り値: [(selection, score, signature, ids), ...] score降順

    ★要点：
      RoleOption が複数グループを持つ場合でも、
      「要求数を超えるグループが出ない」ようにする。
      つまり、選ぶ RoleOption の groups が、すべて remaining に残ってるときだけ許可する。
    """
    dmin, dmax = difficulty_range

    # ジャンルの厳密ルール（和だけ中華を少し混ぜる）
    allowed_genres, genre_bonus_map = _genre_policy(preferred_genre, base_genre)

    needed: List[str] = []
    for g in GROUPS:
        needed += [g] * max(0, int(counts.get(g, 0)))

    target_dish_count = sum(max(0, int(v)) for v in counts.values())
    if target_dish_count <= 0:
        return []

    unique: Dict[str, Tuple[List[Tuple[MenuItem, RoleOption]], int, str, List[int]]] = {}

    for _ in range(tries):
        remaining = needed[:]          # ここが「まだ必要な枠」のマルチセット
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

                # ★ジャンルの厳密フィルタ（和だけ中華を許す、洋/中は厳密）
                if allowed_genres is not None and it.genre not in allowed_genres:
                    continue

                # ★ジャンルボーナス（weights用）
                genre_bonus = 1.0
                if genre_bonus_map:
                    genre_bonus = genre_bonus_map.get(it.genre, 0.9)

                for opt in it.role_options:
                    if target not in opt.groups:
                        continue

                    # ★過剰カバー禁止：
                    # その opt が持つ groups のどれかが remaining に無いなら、
                    # それは「要求数を超える」ので候補から外す。
                    # 例: remaining=["主食"] のとき opt.groups=["主菜","主食"] はNG
                    if any(gg not in remaining for gg in opt.groups):
                        continue

                    cover = sum(1 for gg in opt.groups if gg in remaining)
                    w = opt.weight * genre_bonus * (1.0 + 0.6 * max(0, cover - 1))
                    cands.append((it, opt, w))

            if not cands:
                selection = []
                break

            it, opt, _w = random.choices(cands, weights=[w for _, _, w in cands], k=1)[0]
            chosen_ids.add(it.id)
            selection.append((it, opt))

            for gg in opt.groups:
                if gg in remaining:
                    remaining.remove(gg)

        if not selection:
            continue
        if remaining:
            continue

        s = score_selection(selection, preferred_genre, target_dish_count)
        sig, ids = _selection_signature_and_ids(selection)

        prev = unique.get(sig)
        if (prev is None) or (s > prev[1]):
            unique[sig] = (selection, s, sig, ids)

    cands2 = list(unique.values())
    cands2.sort(key=lambda x: x[1], reverse=True)
    return cands2[:keep]


def pick_menu_from_candidates(
    candidates: List[Tuple[List[Tuple[MenuItem, RoleOption]], int, str, List[int]]],
    pick_mode: str,
    recent_signatures: List[str],
    last_ids: List[int],
) -> Tuple[List[Tuple[MenuItem, RoleOption]], int, str, List[int]]:
    """
    pick_mode:
      auto      -> だいたい高スコア寄り（従来寄せ）
      microwave -> スコア偏らせない（1のみ）
      usual     -> スコア偏らせない（2-3）
      deluxe    -> やや高スコア優先（2-4）
      chef      -> 高スコア優先（2-5）

    直近の完全一致(sig)と、直近セット(last_ids)への類似度にペナルティを掛けて、同じが続きにくい。
    """
    if not candidates:
        return [], -10**9, "", []

    if pick_mode in ("auto", "deluxe", "chef"):
        pool = candidates[:90]
    else:
        pool = candidates[:]

    scores = [s for _sel, s, _sig, _ids in pool]
    min_s, max_s = min(scores), max(scores)
    denom = (max_s - min_s) if (max_s != min_s) else 1.0

    recent_set = set(recent_signatures or [])
    last_set = set(int(x) for x in (last_ids or []))

    weights: List[float] = []
    for sel, s, sig, ids in pool:
        t = (s - min_s) / denom  # 0..1
        w = 1.0

        if pick_mode == "deluxe":
            w *= 0.7 + 2.6 * (t ** 2)
        elif pick_mode == "chef":
            w *= 0.25 + 4.5 * (t ** 4)
        elif pick_mode == "auto":
            w *= 0.45 + 3.4 * (t ** 3)
        else:
            w *= 1.0

        if sig in recent_set:
            w *= 0.03

        if last_set:
            ids_set = set(int(x) for x in ids)
            overlap = len(ids_set & last_set) / max(1, len(ids_set | last_set))  # 0..1
            w *= max(0.06, 1.0 - 0.82 * overlap)

        weights.append(max(1e-6, w))

    idx = random.choices(range(len(pool)), weights=weights, k=1)[0]
    return pool[idx]


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


def _build_rows_uncached(items3: List[MenuItem]) -> List[Dict[str, str]]:
    rows = []
    for it in items3:
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


@st.cache_data(show_spinner=False)
def build_rows_cached(items_ver: int, item_ids_sig: str) -> List[Dict[str, str]]:
    # items_verが変わったら自動で無効化される
    # item_ids_sigは「いま表示対象のitemsが何か」を表すためのキー（内容に依存せず軽い）
    _ = item_ids_sig
    items_now = load_items_cached(items_ver)
    idset = set(int(x) for x in item_ids_sig.split("-") if x)
    filtered_items = [it for it in items_now if int(it.id) in idset]
    return _build_rows_uncached(filtered_items)


def sort_items(items4: List[MenuItem], sort_key: str, asc: bool) -> List[MenuItem]:
    reverse = not asc
    if sort_key == "新しい順":
        return sorted(items4, key=lambda x: x.id, reverse=reverse)
    if sort_key == "料理名":
        return sorted(items4, key=lambda x: x.name.lower(), reverse=reverse)
    if sort_key == "ジャンル":
        return sorted(items4, key=lambda x: GENRES.index(x.genre) if x.genre in GENRES else 999, reverse=reverse)
    if sort_key == "役割の数":
        return sorted(items4, key=lambda x: len(item_any_groups(x)), reverse=reverse)
    if sort_key == "面倒くささ":
        return sorted(items4, key=lambda x: int(x.difficulty), reverse=reverse)
    return items4


# --- AdSense loader を末尾に挿す（表示は別。Auto Ads/広告ユニット次第） ---
def inject_adsense_loader() -> None:
    # セッション中1回だけ（rerun対策）
    if st.session_state.get("_ads_loaded"):
        return

    client = "ca-pub-7509482435345963"
    js = f"""
    <script>
    (function() {{
      const d = window.parent.document;
      const id = "adsense-loader-{client}";

      // 既に入ってたら何もしない
      if (d.getElementById(id)) return;

      const s = d.createElement("script");
      s.id = id;
      s.async = true;
      s.src = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}";
      s.crossOrigin = "anonymous";

      // ★ head に入れる
      if (d.head) {{
        d.head.appendChild(s);
      }} else {{
        (d.documentElement || d.body).appendChild(s);
      }}
    }})();
    </script>
    """
    components.html(js, height=0)
    st.session_state["_ads_loaded"] = True

# -----------------------------
# UI
# -----------------------------
bootstrap_db_sqlite()

# DB初期化（DDL）はセッション中1回だけ
if "_db_ready" not in st.session_state:
    ensure_db()
    st.session_state["_db_ready"] = True

# itemsキャッシュの世代
if "items_ver" not in st.session_state:
    st.session_state["items_ver"] = 0

st.set_page_config(page_title="献立ガチャ", page_icon="🍚")
inject_adsense_loader()

st.markdown(
    """
<style>
/* ガチャ！ボタンをでかく・太く・目立たせる */
div[data-testid="stButton"] > button[kind="primary"]{
  width: 100%;
  padding: 0.95rem 1.2rem;
  border-radius: 16px;
  font-weight: 800;
  font-size: 1.25rem;
  letter-spacing: 0.02em;
  box-shadow: 0 10px 22px rgba(0,0,0,0.18);
  border: 1px solid rgba(255,255,255,0.25);
  transform: translateY(0);
  transition: transform 120ms ease, box-shadow 120ms ease, filter 120ms ease;
}
div[data-testid="stButton"] > button[kind="primary"]:hover{
  transform: translateY(-1px);
  box-shadow: 0 14px 28px rgba(0,0,0,0.22);
  filter: brightness(1.03);
}
div[data-testid="stButton"] > button[kind="primary"]:active{
  transform: translateY(1px);
  box-shadow: 0 8px 16px rgba(0,0,0,0.18);
}

/* 「ガチャ」セクションの余白を少しだけ整える */
section.main .block-container{
  padding-top: 1.4rem;
}

/* ジャンル/面倒くささ の“選択ボタン”を 2行ぶんの高さに固定して中央寄せ */
div[data-testid="stButton"] > button[kind="secondary"]{
  height: 3.4rem;
  padding: 0.55rem 0.6rem;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  white-space: normal;
  line-height: 1.15;
}

/* ===== 結果（今日の献立）をカード表示で目立たせる ===== */
.result-card{
  border: 2px solid rgba(255,255,255,0.22);
  background: rgba(255,255,255,0.06);
  padding: 1.05rem 1.15rem;
  border-radius: 18px;
  box-shadow: 0 12px 26px rgba(0,0,0,0.18);
  margin-top: 0.7rem;
  margin-bottom: 1.6rem;
}
.result-title{
  font-size: 1.35rem;
  font-weight: 900;
  margin: 0 0 0.7rem 0;
  letter-spacing: 0.01em;
}
.result-item{
  font-size: 1.08rem;
  line-height: 1.45;
  margin: 0.35rem 0;
}
.result-meta{
  margin-top: 0.75rem;
  font-size: 0.92rem;
  opacity: 0.85;
}

/* 区切りの余白 */
hr{
  margin: 2.0rem 0 1.6rem 0;
  border: 0;
  border-top: 1px solid rgba(140,140,140,0.35);
}
</style>
""",
    unsafe_allow_html=True,
)

st.title("🍚 献立ガチャ")

items = load_items_cached(st.session_state["items_ver"])
tab_gacha, tab_edit = st.tabs(["🎲 ガチャ", "🛠 登録・編集"])

# =============================
# タブ1: ガチャ
# =============================
with tab_gacha:
    st.header("🎲 今日の献立を引く")

    # ジャンルの気分（ボタン式）
    if "genre_choice" not in st.session_state:
        st.session_state.genre_choice = "自動"

    st.write("ジャンルの気分（押さなければ自動）")
    g1, g2, g3, g4, g5 = st.columns(5)

    if g1.button("自動", key="btn_genre_auto", use_container_width=True):
        st.session_state.genre_choice = "自動"
        request_scroll("anchor_difficulty")
    if g2.button("和食", key="btn_genre_wa", use_container_width=True):
        st.session_state.genre_choice = "和"
        request_scroll("anchor_difficulty")
    if g3.button("洋食", key="btn_genre_yo", use_container_width=True):
        st.session_state.genre_choice = "洋"
        request_scroll("anchor_difficulty")
    if g4.button("中華", key="btn_genre_chu", use_container_width=True):
        st.session_state.genre_choice = "中"
        request_scroll("anchor_difficulty")
    if g5.button("その他", key="btn_genre_other", use_container_width=True):
        st.session_state.genre_choice = "その他"
        request_scroll("anchor_difficulty")

    st.caption(f"いま: {st.session_state.genre_choice}")
    preferred = st.session_state.genre_choice

    # 面倒くささの気分（ボタン式）
    st.markdown("<div id='anchor_difficulty'></div>", unsafe_allow_html=True)

    if "difficulty_preset" not in st.session_state:
        st.session_state.difficulty_preset = None

    st.write("面倒くささの気分（押さなければ自動）")
    b1, b2, b3, b4 = st.columns(4)

    if b1.button("レンチンばんざい", key="btn_preset_microwave", use_container_width=True):
        st.session_state.difficulty_preset = "microwave"
        request_scroll("anchor_counts")
    if b2.button("いつものごはん", key="btn_preset_usual", use_container_width=True):
        st.session_state.difficulty_preset = "usual"
        request_scroll("anchor_counts")
    if b3.button("ごうかなディナー", key="btn_preset_deluxe", use_container_width=True):
        st.session_state.difficulty_preset = "deluxe"
        request_scroll("anchor_counts")
    if b4.button("シェフのおまかせコース", key="btn_preset_chef", use_container_width=True):
        st.session_state.difficulty_preset = "chef"
        request_scroll("anchor_counts")

    label = {
        None: "自動（1〜5）",
        "microwave": "レンチンばんざい（1のみ）",
        "usual": "いつものごはん（2〜3）",
        "deluxe": "ごうかなディナー（2〜4）",
        "chef": "シェフのおまかせコース（2〜5）",
    }
    st.caption(f"いま: {label.get(st.session_state.difficulty_preset)}")

    if st.session_state.difficulty_preset is not None:
        if st.button("自動に戻す", key="btn_preset_reset"):
            st.session_state.difficulty_preset = None
            st.rerun()

    difficulty_range, pick_mode = resolve_difficulty_preset(st.session_state.difficulty_preset)

    # 品数（変更したらガチャへ）
    st.markdown("<div id='anchor_counts'></div>", unsafe_allow_html=True)

    def on_counts_change():
        request_scroll("anchor_gacha")

    st.write("品数（基本は全部1。0にするとその枠は無し）")
    cA, cB, cC, cD, cE = st.columns(5)
    n_shushoku = cA.selectbox("主食", [0, 1, 2, 3], index=1, key="count_shushoku", on_change=on_counts_change)
    n_shusai = cB.selectbox("主菜", [0, 1, 2, 3], index=1, key="count_shusai", on_change=on_counts_change)
    n_fukusai = cC.selectbox("副菜", [0, 1, 2, 3], index=1, key="count_fukusai", on_change=on_counts_change)
    n_milk = cD.selectbox("乳製品", [0, 1, 2, 3], index=0, key="count_milk", on_change=on_counts_change)
    n_fruit = cE.selectbox("果物", [0, 1, 2, 3], index=0, key="count_fruit", on_change=on_counts_change)

    counts = {
        "主食": int(n_shushoku),
        "主菜": int(n_shusai),
        "副菜": int(n_fukusai),
        "乳製品": int(n_milk),
        "果物": int(n_fruit),
    }

    if "recent_menu_sigs" not in st.session_state:
        st.session_state.recent_menu_sigs = []
    if "last_menu_ids" not in st.session_state:
        st.session_state.last_menu_ids = []

    st.markdown("<div id='anchor_gacha'></div>", unsafe_allow_html=True)

    if st.button("ガチャ！", type="primary", use_container_width=True):
        base_genre = None
        if preferred == "自動":
            bases = feasible_auto_base_genres(items, counts, difficulty_range)
            if bases:
                base_genre = random.choice(bases)
            else:
                if any(it.genre != "その他" for it in items):
                    st.error("自動ジャンルで揃えられる候補が足りない（和/洋/中のどれか + その他 で組めるように登録を増やして）")
                    st.stop()

        candidates = generate_candidates(
            items,
            preferred,
            counts,
            difficulty_range,
            base_genre=base_genre,
        )

        selection, score, sig, ids = pick_menu_from_candidates(
            candidates,
            pick_mode=pick_mode,
            recent_signatures=st.session_state.recent_menu_sigs,
            last_ids=st.session_state.last_menu_ids,
        )

        if not selection:
            st.error("その条件を満たせるだけの候補が足りない。品数を減らすか、登録を増やして")
        else:
            st.session_state.recent_menu_sigs = (st.session_state.recent_menu_sigs + [sig])[-8:]
            st.session_state.last_menu_ids = ids

            auto_genre_line = ""
            if preferred == "自動" and base_genre:
                auto_genre_line = f"ジャンル: {html.escape(base_genre)}（自動 / その他は混ぜる）<br>"

            lines = []
            for it, opt in selection:
                line = (
                    f"・{html.escape(it.name)}"
                    f"（{html.escape(it.genre)} / 面倒くささ:{int(it.difficulty)} / 役割: {'・'.join(html.escape(x) for x in opt.groups)}）"
                )
                lines.append(f"<div class='result-item'>{line}</div>")

            st.markdown("<div id='anchor_result'></div>", unsafe_allow_html=True)

            st.markdown(
                f"""
<div class="result-card">
  <div class="result-title">今日の献立</div>
  <div class="result-meta">{auto_genre_line}スコア: {int(score)}</div>
  {''.join(lines)}
</div>
""",
                unsafe_allow_html=True,
            )

            request_scroll("anchor_result")

    run_scroll_if_needed()

# =============================
# タブ2: 登録・編集
# =============================
with tab_edit:
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
                    # 追加でDB内容が変わるのでキャッシュ世代を進める
                    st.session_state["items_ver"] += 1
                    st.success("追加しました")
                    st.rerun()
                except Exception as e:
                    st.error("同じ名前がもうあるか、DBエラーが出たみたい。別名にしてみて")
                    st.caption(str(e)[:200])

        if save_disabled:
            st.caption("追加キーが合ってないと保存できないニャ")

    st.divider()
    st.header("📚 登録済みメニュー")

    # itemsはキャッシュから来るので、ここで最新を取り直す（世代が変わった場合に追従）
    items = load_items_cached(st.session_state["items_ver"])

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

        # build_rowsは整形コストが地味に重いので、対象IDの署名でキャッシュ
        item_ids_sig = "-".join(str(int(it.id)) for it in filtered)
        rows = build_rows_cached(st.session_state["items_ver"], item_ids_sig)
        st.dataframe(rows, use_container_width=True, hide_index=True)

        with st.expander("管理（難易度編集・削除）", expanded=False):
            if not ADMIN_KEY:
                st.caption("ADMIN_KEY が未設定だから管理はロック中")
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
                        st.session_state["items_ver"] += 1
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
                            st.session_state["items_ver"] += 1
                            st.success("消しました")
                            st.rerun()

inject_adsense_loader()

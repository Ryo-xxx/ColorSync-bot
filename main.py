# main.py
import os
import asyncio
import re
import hashlib
from typing import Optional, List

import discord  # type: ignore
from discord import app_commands  # type: ignore
from discord.ext import commands  # type: ignore

from aiohttp import web  # type: ignore
from itsdangerous import URLSafeSerializer, BadSignature  # type: ignore
from dotenv import load_dotenv  # type: ignore
from urllib.parse import urlparse

# ========== 環境変数 ==========
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN が設定されていません。Render の Environment に設定して再デプロイしてね。")

ALLOW_ORIGIN_RAW = os.getenv("ALLOW_ORIGIN", "https://example.com").strip()
WEB_SECRET = os.getenv("WEB_SECRET", "change-me").strip()
PORT = int(os.getenv("PORT", "10000"))

GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
PROTECTED_ROLE_NAMES = [s.strip() for s in os.getenv("PROTECTED_ROLE_NAMES", "").split(",") if s.strip()]
PROTECTED_ROLE_IDS = [int(s.strip()) for s in os.getenv("PROTECTED_ROLE_IDS", "").split(",") if s.strip().isdigit()]

# ページのオリジンだけ抽出（パスがついてもOKにする）
parsed = urlparse(ALLOW_ORIGIN_RAW)
CORS_ALLOW_ORIGIN = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ALLOW_ORIGIN_RAW

# ========== 署名器 ==========
signer = URLSafeSerializer(WEB_SECRET, salt="color")

# ========== Bot 基本 ==========
intents = discord.Intents.default()
intents.members = True  # Server Members Intent を Dev Portal で ON
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ギルド同期対象（あれば高速反映）
GUILD_IDS: List[int] = []
if GUILD_ID_RAW:
    for s in GUILD_ID_RAW.split(","):
        s = s.strip()
        if s.isdigit():
            GUILD_IDS.append(int(s))

# ========== 命名規則（新方式：短いハッシュで紐付け） ==========
# 旧方式   : "<任意名>-<user_id>"（末尾が18桁前後の数字）
# 新方式   : "<任意名>-<hash6>"   （uid と WEB_SECRET から作る6桁ハッシュ）
# 目的     : 見た目にIDを出さずに「誰のロールか」特定できるようにする
ID_SUFFIX_PATTERN = re.compile(r"-([0-9]{15,25})$")           # 旧方式の検出
HASH_SUFFIX_PATTERN = re.compile(r"-([0-9a-f]{6})$", re.I)    # 新方式の検出

def uid_hash6(uid: int) -> str:
    raw = f"{uid}:{WEB_SECRET}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:6]

def pretty_role_name(name: str) -> str:
    """末尾の -<id> / -<hash6> を見た目から取り除いた表示用"""
    if ID_SUFFIX_PATTERN.search(name):
        return ID_SUFFIX_PATTERN.sub("", name)
    if HASH_SUFFIX_PATTERN.search(name):
        return HASH_SUFFIX_PATTERN.sub("", name)
    return name

def new_personal_name(base: str, uid: int) -> str:
    """ユーザー入力の表示名に短ハッシュを付ける（100文字制限を考慮）"""
    suffix = "-" + uid_hash6(uid)
    base = base.strip()
    # 100文字超えないようにトリム（Discordのロール名上限は100）
    max_base = 100 - len(suffix)
    if len(base) > max_base:
        base = base[:max_base]
    return base + suffix

# ========== 共通ユーティリティ ==========
def is_protected(role: discord.Role) -> bool:
    return role.id in PROTECTED_ROLE_IDS or role.name in PROTECTED_ROLE_NAMES

def ensure_manageable(guild: discord.Guild, role: discord.Role):
    """Botがそのロールを編集できるか（階層と権限）をチェック"""
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("Botに 'Manage Roles' 権限がありません。")
    if role >= me.top_role:
        raise RuntimeError("Botのロール位置が対象ロール以下です。サーバー設定でBotロールを上に移動してください。")
    if is_protected(role):
        raise RuntimeError("保護対象のロールは変更できません。")

def find_personal_role(member: discord.Member) -> Optional[discord.Role]:
    """
    このメンバーの「個人色ロール」を特定する。
    優先順：
      1) 末尾が -<hash6> で、hash6(uid) と一致
      2) 末尾が -<user_id>（旧方式）
    まずはメンバー所持ロールを見て、無ければギルド全体から検索。
    """
    gid_hash = uid_hash6(member.id)

    # まずは所持ロールから
    for r in member.roles:
        n = r.name
        if n.endswith("-" + gid_hash):
            return r
        if n.endswith("-" + str(member.id)):
            return r

    # 念のためギルド全体からも探す
    for r in member.guild.roles:
        n = r.name
        if n.endswith("-" + gid_hash):
            return r
        if n.endswith("-" + str(member.id)):
            return r

    return None

async def create_or_update_personal_role(member: discord.Member, rgb_value: int) -> discord.Role:
    """
    既存があれば色更新、なければ作成する（/color_web 用）
    新規作成時は NameColor-<hash6> で作る。
    """
    guild = member.guild
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("Botに 'Manage Roles' 権限がありません。")

    role = find_personal_role(member)
    if role is None:
        role_name = new_personal_name("NameColor", member.id)
        role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(rgb_value),
            permissions=discord.Permissions.none(),
            reason="Create personal color role",
            hoist=False,
            mentionable=False,
        )
    else:
        ensure_manageable(guild, role)
        await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color")

    # 未付与なら付ける
    if role not in member.roles:
        await member.add_roles(role, reason="Attach personal color role")

    return role

async def update_only_color(member: discord.Member, rgb_value: int) -> discord.Role:
    """
    既存ロールが無いときはエラーにする「色だけ変更」用
    """
    role = find_personal_role(member)
    if role is None:
        raise RuntimeError("あなたの個人ロールが見つかりません。まずは /color_web で作成してね。")
    ensure_manageable(member.guild, role)
    await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color (only)")
    return role

async def rename_personal_role(member: discord.Member, new_base_name: str) -> discord.Role:
    """
    個人ロールの表示名を変更。
    ・旧方式（-<id>）で持っていても、新方式（-<hash6>）名に統一。
    """
    role = find_personal_role(member)
    if role is None:
        raise RuntimeError("あなたの個人ロールが見つかりません。まずは /color_web で作成してね。")
    ensure_manageable(member.guild, role)

    new_name = new_personal_name(new_base_name, member.id)
    await role.edit(name=new_name, reason="Rename personal color role")
    return role

async def migrate_personal_role_name(member: discord.Member) -> Optional[discord.Role]:
    """
    旧式名（…-<id>）を見つけたら、新方式（…-<hash6>）へ付け替える。
    """
    role = find_personal_role(member)
    if role is None:
        return None

    # 旧式なら置き換え
    if role.name.endswith("-" + str(member.id)):
        vis = pretty_role_name(role.name)  # 旧名から末尾を外した見た目
        new_name = new_personal_name(vis or "NameColor", member.id)
        ensure_manageable(member.guild, role)
        await role.edit(name=new_name, reason="Migrate role name to hash suffix")
    return role

# ========== AIOHTTP (API) ==========
routes = web.RouteTableDef()

def corsify(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "content-type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    return resp

@routes.get("/")
async def health(_: web.Request):
    return web.Response(text="ok")

@routes.options("/apply")
async def preflight(_: web.Request):
    return corsify(web.Response())

@routes.post("/apply")
async def apply(request: web.Request):
    """
    JSON: { "t": "<signed token>", "hex": "#RRGGBB" }
    tokenは { "g": guild_id, "u": user_id } を署名したもの
    """
    try:
        data = await request.json()
        token = str(data.get("t", "")).strip()
        hexv = str(data.get("hex", "")).lstrip("#").strip()

        payload = signer.loads(token)  # BadSignature -> except
        gid = int(payload["g"])
        uid = int(payload["u"])

        guild = bot.get_guild(gid)
        if guild is None:
            return corsify(web.json_response({"ok": False, "msg": "guild not found"}, status=404))

        member = guild.get_member(uid) or await guild.fetch_member(uid)

        rgb = int(hexv, 16)
        role = await create_or_update_personal_role(member, rgb)
        return corsify(web.json_response({
            "ok": True,
            "msg": f"applied #{hexv.lower()}",
            "role": role.name,
            "display": pretty_role_name(role.name),
        }))

    except BadSignature:
        return corsify(web.json_response({"ok": False, "msg": "invalid token"}, status=400))
    except ValueError:
        return corsify(web.json_response({"ok": False, "msg": "invalid hex"}, status=400))
    except Exception as e:
        return corsify(web.json_response({"ok": False, "msg": f"apply error: {e}"}, status=500))

app = web.Application()
app.add_routes(routes)

# ========== スラッシュコマンド ==========
@tree.command(name="color_web", description="外部ページで色を選べるリンクを送る（自分専用）")
async def color_web_cmd(interaction: discord.Interaction):
    token = signer.dumps({"g": interaction.guild.id, "u": interaction.user.id})
    url = f"{ALLOW_ORIGIN_RAW}?t={token}"

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページ）", url=url))
    await interaction.response.send_message(
        "外部ページで色を選んで『Discordへ適用』を押してね！",
        view=view,
        ephemeral=True
    )

@tree.command(name="color_set", description="既存の自分用ロールの色だけ変更（ロール名はそのまま）")
@app_commands.describe(hex="#RRGGBB 形式の色（例：#ff99cc）")
async def color_set_cmd(interaction: discord.Interaction, hex: str):
    await interaction.response.defer(ephemeral=True)
    try:
        rgb = int(hex.lstrip("#"), 16)
        role = await update_only_color(interaction.user, rgb)
        await interaction.followup.send(
            f"✅ ロール **{pretty_role_name(role.name)}** の色を `{hex}` に変更したよ。", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ 変更できなかったよ：{e}", ephemeral=True)

@tree.command(name="color_rename", description="既存の自分用ロールの名前を変更（短ハッシュで紐付け維持）")
@app_commands.describe(name="新しく付けたいロール名（例：MyColor）")
async def color_rename_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # 末尾に -<id> / -<hash> を入れる必要はない（自動付与）
        if ID_SUFFIX_PATTERN.search(name) or HASH_SUFFIX_PATTERN.search(name):
            raise RuntimeError("末尾の -<何か> は付けないでOK！純粋なロール名だけ入れてね。")
        role = await rename_personal_role(interaction.user, name)
        await interaction.followup.send(
            f"✅ ロール名を **{pretty_role_name(role.name)}** に変更したよ。", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ 変更できなかったよ：{e}", ephemeral=True)

@tree.command(name="color_fixname", description="旧式（-ID）名のロールを新方式（-ハッシュ）名に整理する")
async def color_fixname_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        role = await migrate_personal_role_name(interaction.user)
        if role is None:
            await interaction.followup.send("色ロールが見つからないよ。まずは /color_web から作ってね。", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ ロール名を **{pretty_role_name(role.name)}** に整理したよ。", ephemeral=True
            )
    except Exception as e:
        await interaction.followup.send(f"⚠️ 失敗：{e}", ephemeral=True)

# 管理者用：再同期（ギルドに即時反映）
@tree.command(name="resync", description="（管理者）スラッシュコマンドを再同期する")
@app_commands.checks.has_permissions(administrator=True)
async def resync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if GUILD_IDS:
            total = 0
            for gid in GUILD_IDS:
                guild_obj = discord.Object(id=gid)
                tree.copy_global_to(guild=guild_obj)
                synced = await tree.sync(guild=guild_obj)
                total += len(synced)
            await interaction.followup.send(f"🔄 再同期しました（合計 {total} 件）", ephemeral=True)
        else:
            synced = await tree.sync()
            await interaction.followup.send(f"🔄 グローバル {len(synced)} 件を再同期しました（反映に時間がかかる場合あり）", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ 失敗：{e}", ephemeral=True)

# ========== 起動時処理 ==========
@bot.event
async def on_ready():
    print(f"[READY] {bot.user} ({bot.user.id})", flush=True)
    try:
        if GUILD_IDS:
            total = 0
            for gid in GUILD_IDS:
                guild_obj = discord.Object(id=gid)
                tree.copy_global_to(guild=guild_obj)
                synced = await tree.sync(guild=guild_obj)
                total += len(synced)
                print(f"[SYNC] guild={gid} count={len(synced)}", flush=True)
            print(f"[SYNC] done total={total}", flush=True)
        else:
            synced = await tree.sync()
            print(f"[SYNC] global count={len(synced)}", flush=True)
    except Exception as e:
        print("[SYNC-ERROR]", e, flush=True)

async def start_web():
    print("[WEB] binding :10000", flush=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print("[WEB] started :10000", flush=True)

async def main():
    print("[BOOT] starting app...", flush=True)
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

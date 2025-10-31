import os
import asyncio
import re
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

# ========== 共通ユーティリティ ==========
ID_SUFFIX_PATTERN = re.compile(r"-([0-9]{15,25})$")  # ロール末尾の -<user_id> を特定

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

def personal_role_name_for(member: discord.Member, base: Optional[str] = None) -> str:
    """
    個人ロールの命名規則：
    - 末尾に必ず `-<user_id>` を付与して本人と紐付け
    - base を渡せば `base-<id>`、None の場合は既存検出用
    """
    if base is None:
        base = "NameColor"
    return f"{base}-{member.id}"

def find_personal_role(member: discord.Member) -> Optional[discord.Role]:
    """
    ユーザーIDで終わるロールを探す（*-<user_id>）
    ロール名がリネームされても末尾IDで追跡可能
    """
    suffix = f"-{member.id}"
    for r in member.guild.roles:
        if r.name.endswith(suffix):
            return r
    return None

async def create_or_update_personal_role(member: discord.Member, rgb_value: int) -> discord.Role:
    """
    既存があれば色更新、なければ作成する（/color_web 用）
    """
    guild = member.guild
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise RuntimeError("Botに 'Manage Roles' 権限がありません。")

    role = find_personal_role(member)
    if role is None:
        # 新規作成（デフォルト名は NameColor-<id>）
        role = await guild.create_role(
            name=personal_role_name_for(member, "NameColor"),
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
    個人ロールの表示名を変更（末尾の -<user_id> は維持して特定性を担保）
    """
    role = find_personal_role(member)
    if role is None:
        raise RuntimeError("あなたの個人ロールが見つかりません。まずは /color_web で作成してね。")
    ensure_manageable(member.guild, role)

    # 末尾IDは維持して先頭を入れ替え
    new_name = f"{new_base_name}-{member.id}"
    if len(new_name) > 100:
        raise RuntimeError("ロール名が長すぎます（100文字以内）")

    await role.edit(name=new_name, reason="Rename personal color role")
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
        return corsify(web.json_response({"ok": True, "msg": f"applied #{hexv.lower()}", "role": role.name}))

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
        await interaction.followup.send(f"✅ ロール **{role.name}** の色を `{hex}` に変更したよ。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ 変更できなかったよ：{e}", ephemeral=True)

@tree.command(name="color_rename", description="既存の自分用ロールの名前を変更（末尾のIDは維持）")
@app_commands.describe(name="新しく付けたいロール名（例：MyColor）")
async def color_rename_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # 末尾IDは自動付与するので、ユーザー入力には ID を含めない想定
        if ID_SUFFIX_PATTERN.search(name):
            raise RuntimeError("末尾に -<id> を含めない名前を入力してね。ID部分は自動で付きます。")
        role = await rename_personal_role(interaction.user, name)
        await interaction.followup.send(f"✅ ロール名を **{role.name}** に変更したよ。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ 変更できなかったよ：{e}", ephemeral=True)

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

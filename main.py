import os
import asyncio
import logging
from urllib.parse import urlparse

import discord  # type: ignore
from discord import app_commands  # type: ignore
from discord.ext import commands  # type: ignore
from aiohttp import web  # type: ignore
from itsdangerous import URLSafeSerializer, BadSignature  # type: ignore
from dotenv import load_dotenv  # type: ignore

# -----------------------------
# Logging (Renderに確実に出す)
# -----------------------------
logging.basicConfig(level=logging.INFO, force=True)
discord.utils.setup_logging(level=logging.INFO)
print("[BOOT] starting app...", flush=True)

# -----------------------------
# 環境変数
# -----------------------------
load_dotenv()  # ローカル実行時のみ有効。Renderでは無視される想定。

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN が .env/環境変数にありません。")

ALLOW_ORIGIN_RAW = os.getenv("ALLOW_ORIGIN", "https://example.com").strip()
WEB_SECRET = os.getenv("WEB_SECRET", "change-me").strip()
PORT = int(os.getenv("PORT", "10000"))

GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()  # カンマ区切り可
PROTECTED_ROLE_NAMES = [s.strip() for s in os.getenv("PROTECTED_ROLE_NAMES", "").split(",") if s.strip()]
PROTECTED_ROLE_IDS = [int(s.strip()) for s in os.getenv("PROTECTED_ROLE_IDS", "").split(",") if s.strip().isdigit()]

# CORS: スキーム+ホストのみ
parsed = urlparse(ALLOW_ORIGIN_RAW)
CORS_ALLOW_ORIGIN = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ALLOW_ORIGIN_RAW

# 署名器（外部ページ→Discord用トークン）
signer = URLSafeSerializer(WEB_SECRET, salt="color")

# -----------------------------
# Discord Bot 準備
# -----------------------------
intents = discord.Intents.default()
intents.members = True  # 個別ロール付与で必要（Dev PortalのServer Members IntentもONに）
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

GUILD_IDS: list[int] = []
if GUILD_ID_RAW:
    for s in GUILD_ID_RAW.split(","):
        s = s.strip()
        if s.isdigit():
            GUILD_IDS.append(int(s))

# -----------------------------
# 色ロール付与ロジック
# -----------------------------
async def apply_member_color(member: discord.Member, rgb_value: int):
    """
    ユーザー専用の色役職 NameColor-<user_id> を作成/更新し、ユーザーに付与する。
    """
    guild = member.guild
    me = guild.me or await guild.fetch_member(bot.user.id)  # type: ignore

    if not me.guild_permissions.manage_roles:
        raise RuntimeError("Botに『役職の管理(Manage Roles)』権限がありません。")

    role_name = f"NameColor-{member.id}"
    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(rgb_value),
            permissions=discord.Permissions.none(),
            reason="Create personal color role",
            hoist=False,
            mentionable=False,
        )
    else:
        if (role.id in PROTECTED_ROLE_IDS) or (role.name in PROTECTED_ROLE_NAMES):
            raise RuntimeError("保護対象の役職には変更できません。")
        await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color role")

    # 役職階層チェック（Botより上は付与不可）
    if role >= me.top_role:
        raise RuntimeError("作成/対象ロールがBotの最上位ロール以上です。サーバー設定でBotロールを上に移動してください。")

    if role not in member.roles:
        await member.add_roles(role, reason="Attach personal color role")

# -----------------------------
# AIOHTTP (Web API)
# -----------------------------
routes = web.RouteTableDef()

def corsify(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "content-type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
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
    token = signer.dumps({ "g": guild_id, "u": user_id })
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
        await apply_member_color(member, rgb)
        return corsify(web.json_response({"ok": True, "msg": f"applied #{hexv.lower()}"}))

    except BadSignature:
        return corsify(web.json_response({"ok": False, "msg": "invalid token"}, status=400))
    except ValueError:
        return corsify(web.json_response({"ok": False, "msg": "invalid hex"}, status=400))
    except Exception as e:
        return corsify(web.json_response({"ok": False, "msg": f"apply error: {e}"}, status=500))

app = web.Application()
app.add_routes(routes)

# -----------------------------
# スラッシュコマンド
# -----------------------------
@tree.command(name="color_web", description="外部ページで色を選べるリンクを送る（自分専用）")
async def color_web_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってね。", ephemeral=True)
        return

    token = signer.dumps({"g": interaction.guild.id, "u": interaction.user.id})
    url = f"{ALLOW_ORIGIN_RAW}?t={token}"

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページ）", url=url))
    await interaction.response.send_message(
        "外部ページで色を選んで『Discordへ適用』を押してね！",
        view=view,
        ephemeral=True
    )

@tree.command(name="resync", description="スラッシュコマンドを同期し直す（管理者）")
@app_commands.checks.has_permissions(administrator=True)
async def resync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD_IDS:
            total = 0
            for gid in GUILD_IDS:
                g = discord.Object(id=gid)
                synced = await tree.sync(guild=g)
                total += len(synced)
            await interaction.followup.send(f"ギルド同期を完了: {total} 件", ephemeral=True)
        else:
            synced = await tree.sync()
            await interaction.followup.send(f"グローバル同期を完了: {len(synced)} 件（反映に時間がかかることがあります）", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"同期失敗: {e}", ephemeral=True)

# -----------------------------
# 起動時処理（同期）
# -----------------------------
@bot.event
async def on_ready():
    print(f"[READY] {bot.user} ({bot.user.id})", flush=True)
    print("ℹ️  招待URLは Scopes: bot + applications.commands を必ず含める。", flush=True)
    print("ℹ️  Botロールを作成ロールより上に配置（Manage Roles権限も付与）。", flush=True)
    try:
        if GUILD_IDS:
            total = 0
            for gid in GUILD_IDS:
                guild_obj = discord.Object(id=gid)
                synced = await tree.sync(guild=guild_obj)
                total += len(synced)
                print(f"[SYNC] guild={gid} count={len(synced)}", flush=True)
            print(f"[SYNC] done total={total}", flush=True)
        else:
            synced = await tree.sync()
            print(f"[SYNC] global count={len(synced)}（伝播に時間がかかる場合があります）", flush=True)
    except Exception as e:
        print("[SYNC-ERROR]", e, flush=True)

# -----------------------------
# Webサーバ & Bot起動
# -----------------------------
async def start_web():
    print(f"[WEB] binding :{PORT}", flush=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"[WEB] started :{PORT}", flush=True)

async def main():
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

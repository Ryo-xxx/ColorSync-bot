# -*- coding: utf-8 -*-
"""
ColorSync-bot （ロールカラー変更ボット / Render対応）
- Discordのスラッシュコマンド `/color_web` で署名付きURLを配布
- GitHub Pagesから選択色をPOSTすると、
  メンバー専用の「NameColor-<USER_ID>」ロールを作成/更新して反映
- 重要ロールは一切触らない（個人カラー専用ロールのみ操作）
起動: python main.py
"""

import os
import asyncio
import logging
from typing import List

import discord
from discord.ext import commands
from discord import app_commands

from aiohttp import web
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv

# ---------- Env ----------
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 任意（指定すると同期が爆速）
WEB_SECRET = os.getenv("WEB_SECRET", "change-me")
ALLOW_ORIGIN = os.getenv("ALLOW_ORIGIN", "https://example.com")
PORT = int(os.getenv("PORT", "10000"))

# カンマ区切りで複数オリジンを許可できる
ALLOW_ORIGINS: List[str] = [o.strip().rstrip('/') for o in ALLOW_ORIGIN.split(",") if o.strip()]

# 重要ロールは操作しない（保護）: 名前 or ID をカンマ区切りで設定可能
PROTECTED_ROLE_NAMES = [s.strip() for s in os.getenv("PROTECTED_ROLE_NAMES", "admin,administrator,mod,管理者").split(",") if s.strip()]
PROTECTED_ROLE_IDS = [int(s) for s in os.getenv("PROTECTED_ROLE_IDS", "").split(",") if s.strip().isdigit()]

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN が未設定です。.env を確認してください。" )

signer = URLSafeSerializer(WEB_SECRET, salt="color-sync")

# ---------- Discord ----------
intents = discord.Intents.default()
intents.members = True  # メンバー取得に必要
bot = commands.Bot(command_prefix="!", intents=intents)

# ログ
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("colorsync")

PERSONAL_ROLE_PREFIX = "NameColor-"


async def get_or_create_personal_role(member: discord.Member) -> discord.Role:
    """ユーザー専用のカラー用ロールを取得 or 作成"""
    role_name = f"{PERSONAL_ROLE_PREFIX}{member.id}"
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role:
        return role

    # 新規作成（権限なし）
    role = await member.guild.create_role(
        name=role_name,
        colour=discord.Colour(0),
        permissions=discord.Permissions.none(),
        reason="Create personal color role",
        mentionable=False,
        hoist=False,
    )
    # 付与
    await member.add_roles(role, reason="Attach personal color role")
    return role


async def apply_member_color(member: discord.Member, rgb_value: int):
    """個人カラー用ロールの色を更新（他ロールは一切触らない）"""
    role = await get_or_create_personal_role(member)

    # Botロールより上には動かせないので、色変更だけ行う
    await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color")

    # 念のためロールを付与（未付与だった場合）
    await member.add_roles(role, reason="Ensure personal color role attached")


def is_protected(role: discord.Role) -> bool:
    """保護対象ロール判定（今回の仕様では未使用。必要なら活用してね）"""
    return (role.id in PROTECTED_ROLE_IDS) or (role.name.lower() in (n.lower() for n in PROTECTED_ROLE_NAMES))


# ---------- Web (aiohttp) ----------
routes = web.RouteTableDef()


def add_cors(resp: web.StreamResponse) -> web.StreamResponse:
    origin = ALLOW_ORIGINS[0] if ALLOW_ORIGINS else "*"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Headers"] = "content-type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@routes.get("/health")
async def health(_):
    return web.json_response({"ok": True, "msg": "alive"})


@routes.options("/apply")
async def preflight(_):
    return add_cors(web.Response(status=204))


@routes.post("/apply")
async def apply(request: web.Request):
    try:
        body = await request.json()
        token = str(body.get("t", ""))
        hexv = str(body.get("hex", "")).lstrip("#")
        payload = signer.loads(token)
    except BadSignature:
        return add_cors(web.json_response({"ok": False, "msg": "invalid token"}, status=401))
    except Exception as e:
        return add_cors(web.json_response({"ok": False, "msg": f"bad request: {e}"}, status=400))

    if len(hexv) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hexv):
        return add_cors(web.json_response({"ok": False, "msg": "hex must be 6 digits"}, status=400))

    guild = bot.get_guild(int(payload["g"]))
    member = guild.get_member(int(payload["u"])) if guild else None
    if not member:
        return add_cors(web.json_response({"ok": False, "msg": "member not found"}, status=404))

    try:
        rgb = int(hexv, 16)
        await apply_member_color(member, rgb)
        return add_cors(web.json_response({"ok": True, "msg": f"applied #{hexv.lower()}"}))
    except discord.Forbidden:
        return add_cors(web.json_response({"ok": False, "msg": "bot role is too low in hierarchy"}, status=403))
    except Exception as e:
        log.exception("apply error")
        return add_cors(web.json_response({"ok": False, "msg": f"apply error: {e}"}))

app = web.Application()
app.add_routes(routes)


# ---------- Slash Command ----------
@bot.tree.command(name="color_web", description="外部ページ（GitHub Pages）で色を選ぶリンクを表示します")
async def color_web(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で実行してね。", ephemeral=True)
        return
    token = URLSafeSerializer(WEB_SECRET, salt="color-sync").dumps({"g": interaction.guild.id, "u": interaction.user.id})
    origin = ALLOW_ORIGINS[0] if ALLOW_ORIGINS else "https://example.com"
    url = f"{origin}/Color-code-converter/?t={token}"
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページを開く）", url=url))
    await interaction.response.send_message(
        "外部ページで色を選んで『Discordへ適用』を押してね！",
        view=view, ephemeral=True
    )


@bot.event
async def on_ready():
    log.info("✅ Logged in as %s", bot.user)
    try:
        if GUILD_ID:
            await bot.tree.sync(guild=discord.Object(id=int(GUILD_ID)))
        else:
            await bot.tree.sync()
        log.info("✅ Slash commands synced")
    except Exception as e:
        log.error("❌ Slash command sync failed: %s", e)


# ---------- Runner (Bot + Web) ----------
async def start_web_app():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("🌐 Web server started on :%s", PORT)


async def amain():
    await asyncio.gather(
        start_web_app(),
        bot.start(TOKEN),
    )


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Shutting down...")


if __name__ == "__main__":
    main()

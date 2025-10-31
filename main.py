# main.py — ColorSync-bot (Render対応版)
import os
import asyncio
import json
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
WEB_SECRET = os.getenv("WEB_SECRET", "ColorSyncBot555")
ALLOW_ORIGIN = os.getenv("ALLOW_ORIGIN", "https://example.com")
PORT = int(os.getenv("PORT", 10000))

signer = URLSafeSerializer(WEB_SECRET, salt="color")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def apply_member_color(member: discord.Member, rgb_value: int):
    role_name = f"NameColor-{member.id}"
    role = discord.utils.get(member.guild.roles, name=role_name)
    if not role:
        role = await member.guild.create_role(
            name=role_name,
            colour=discord.Colour(rgb_value),
            reason="Personal color role",
            permissions=discord.Permissions.none(),
        )
        await member.add_roles(role)
    else:
        await role.edit(colour=discord.Colour(rgb_value))
        await member.add_roles(role)

routes = web.RouteTableDef()

def cors(resp: web.StreamResponse):
    resp.headers["Access-Control-Allow-Origin"] = ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "content-type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp

@routes.options("/apply")
async def preflight(request):
    return cors(web.Response())

@routes.post("/apply")
async def apply(request):
    try:
        body = await request.json()
        token = body.get("t", "")
        hexv = str(body.get("hex", "")).lstrip("#")
        payload = signer.loads(token)
    except BadSignature:
        return cors(web.json_response({"ok": False, "msg": "invalid token"}, status=400))
    except Exception as e:
        return cors(web.json_response({"ok": False, "msg": f"error: {e}"}, status=400))

    guild = bot.get_guild(int(payload["g"]))
    member = guild.get_member(int(payload["u"])) if guild else None
    if not member:
        return cors(web.json_response({"ok": False, "msg": "member not found"}, status=404))

    try:
        rgb = int(hexv, 16)
        await apply_member_color(member, rgb)
        return cors(web.json_response({"ok": True, "msg": f"applied #{hexv.lower()}"}))
    except Exception as e:
        return cors(web.json_response({"ok": False, "msg": f"apply error: {e}"}))

app = web.Application()
app.add_routes(routes)

@bot.tree.command(name="color_web", description="外部ページで色を選ぶリンクを表示")
async def color_web(interaction: discord.Interaction):
    token = signer.dumps({"g": interaction.guild.id, "u": interaction.user.id})
    url = f"{ALLOW_ORIGIN}/?t={token}"
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページ）", url=url))
    await interaction.response.send_message("外部ページで色を選んで『Discordへ適用』を押してね！", view=view, ephemeral=True)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

def main():
    loop = asyncio.get_event_loop()
    loop.create_task(web._run_app(app, host="0.0.0.0", port=PORT))
    bot.run(TOKEN)

if __name__ == "__main__":
    main()

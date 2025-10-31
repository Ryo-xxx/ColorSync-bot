import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv
from urllib.parse import urlparse

# ==========
# 環境変数
# ==========
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN が設定されていません。")

ALLOW_ORIGIN_RAW = os.getenv("ALLOW_ORIGIN", "https://example.com").strip()
WEB_SECRET = os.getenv("WEB_SECRET", "change-me").strip()
PORT = int(os.getenv("PORT", "10000"))

# Optional
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
PROTECTED_ROLE_NAMES = [s.strip() for s in os.getenv("PROTECTED_ROLE_NAMES", "").split(",") if s.strip()]
PROTECTED_ROLE_IDS = [int(s.strip()) for s in os.getenv("PROTECTED_ROLE_IDS", "").split(",") if s.strip().isdigit()]

# CORSに使う "オリジン" はスキーム+ホスト部分だけ抜く（GitHub Pages のようにパスを含むURLを渡されてもOKにする）
parsed = urlparse(ALLOW_ORIGIN_RAW)
CORS_ALLOW_ORIGIN = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ALLOW_ORIGIN_RAW

# ==========
# 署名器
# ==========
signer = URLSafeSerializer(WEB_SECRET, salt="color")

# ==========
# Discord Bot
# ==========
intents = discord.Intents.default()
intents.members = True  # メンバー取得が必要（Dev Portal で "Server Members Intent" をONにしておく）
bot = commands.Bot(command_prefix="!", intents=intents)

# ギルド同期対象
GUILD_IDS = []
if GUILD_ID_RAW:
    for s in GUILD_ID_RAW.split(","):
        s = s.strip()
        if s.isdigit():
            GUILD_IDS.append(int(s))

# ==========
# 役職の作成/更新
# ==========
async def apply_member_color(member: discord.Member, rgb_value: int):
    """
    ユーザー専用の色役職 NameColor-<user_id> を作成/更新し、ユーザーに付与する。
    """
    role_name = f"NameColor-{member.id}"
    guild = member.guild

    # Botが役職を扱えるか念のためチェック
    me = guild.me
    if not guild.me.guild_permissions.manage_roles:
        raise RuntimeError("Botに 'Manage Roles'（役職の管理）権限がありません。")

    # 既存の同名ロールを取得
    role = discord.utils.get(guild.roles, name=role_name)

    # 役職作成または色変更
    if role is None:
        role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(rgb_value),
            permissions=discord.Permissions.none(),
            reason="Personal color role",
            hoist=False,
            mentionable=False,
        )
    else:
        # Protectedロールの安全チェック（基本該当しないはずだが保険）
        if (role.id in PROTECTED_ROLE_IDS) or (role.name in PROTECTED_ROLE_NAMES):
            raise RuntimeError("保護対象の役職には変更できません。")
        await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color role")

    # ユーザーに付与（未付与なら）
    if role not in member.roles:
        await member.add_roles(role, reason="Attach personal color role")

# ==========
# AIOHTTP (API)
# ==========
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
    tokenは { "g": guild_id, "u": user_id } を署名したもの
    """
    try:
        data = await request.json()
        token = str(data.get("t", "")).strip()
        hexv = str(data.get("hex", "")).lstrip("#").strip()

        # トークン検証
        payload = signer.loads(token)  # BadSignature -> except
        gid = int(payload["g"])
        uid = int(payload["u"])

        # 対象取得
        guild = bot.get_guild(gid)
        if guild is None:
            return corsify(web.json_response({"ok": False, "msg": "guild not found"}, status=404))

        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                return corsify(web.json_response({"ok": False, "msg": "member not found"}, status=404))

        # 色適用
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

# ==========
# スラッシュコマンド
# ==========
@bot.tree.command(name="color_web", description="外部ページで色を選べるリンクを送る（自分専用）")
async def color_web_cmd(interaction: discord.Interaction):
    # 対象ギルドで同期していないとMissing Accessになる点に注意
    token = signer.dumps({"g": interaction.guild.id, "u": interaction.user.id})

    # ALLOW_ORIGIN は「ページのフルURL」を想定。?t= を付けて渡す
    url = f"{ALLOW_ORIGIN_RAW}?t={token}"

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページ）", url=url))
    await interaction.response.send_message(
        "外部ページで色を選んで『Discordへ適用』を押してね！",
        view=view,
        ephemeral=True
    )

# ==========
# 同期と起動
# ==========
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

    # ギルド同期（速い/確実）。指定が無ければグローバル同期（数分かかることあり）
    if GUILD_IDS:
        for gid in GUILD_IDS:
            try:
                guild_obj = discord.Object(id=gid)
                await bot.tree.sync(guild=guild_obj)
                print(f"🌱 Synced commands to guild: {gid}")
            except discord.HTTPException as e:
                print(f"⚠️  Guild sync failed for {gid}: {e}")
    else:
        try:
            await bot.tree.sync()
            print("🌍 Synced commands globally")
        except discord.HTTPException as e:
            print(f"⚠️  Global sync failed: {e}")

async def start_web():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"🌐 HTTP server started on 0.0.0.0:{PORT}")

async def main():
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

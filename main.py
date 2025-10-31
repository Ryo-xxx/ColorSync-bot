# main.py
# ColorSync-bot: 外部ページのカラーピッカーから個人カラーを適用するDiscordボット
# - RenderのWebサービスで動作（PORTにバインド）
# - GUILD_IDは「,」区切りで複数同期OK
# - /color_web, /color_clear を提供

import os
import asyncio
from typing import Optional, Set, List

import discord
from discord import app_commands
from aiohttp import web
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv

# ========= ENV =========
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is missing.")

# 複数ギルド対応（カンマ区切り）
GUILD_IDS_RAW = os.getenv("GUILD_ID", "").strip()
GUILD_IDS: List[int] = []
if GUILD_IDS_RAW:
    for x in GUILD_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            GUILD_IDS.append(int(x))

# 複数オリジン許可（カンマ区切り）
ALLOW_ORIGIN_RAW = os.getenv("ALLOW_ORIGIN", "https://example.com")
ALLOW_ORIGINS: List[str] = [o.strip().rstrip("/") for o in ALLOW_ORIGIN_RAW.split(",") if o.strip()]

WEB_SECRET = os.getenv("WEB_SECRET", "CHANGE_ME_TO_RANDOM_32_64_CHARS")
PORT = int(os.getenv("PORT", "10000"))

def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

PROTECTED_ROLE_NAMES: Set[str] = set(n.lower() for n in _split_csv(os.getenv(
    "PROTECTED_ROLE_NAMES",
    "admin,administrator,mod,Server Booster,管理者,神"
)))
PROTECTED_ROLE_IDS: Set[int] = set(int(x) for x in _split_csv(os.getenv(
    "PROTECTED_ROLE_IDS", ""
)) if x.isdigit())

signer = URLSafeSerializer(WEB_SECRET, salt="colorsync")

# ========= Discord Client =========
intents = discord.Intents.default()
intents.members = True  # 個人ロール付与でメンバー取得が必要
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

PERSONAL_ROLE_PREFIX = "NameColor-"

# ========= AIOHTTP =========
routes = web.RouteTableDef()

def _origin_ok(request: web.Request) -> Optional[str]:
    origin = request.headers.get("Origin", "")
    if not origin:
        return None
    origin = origin.rstrip("/")
    return origin if origin in ALLOW_ORIGINS else None

def _with_cors(resp: web.StreamResponse, allow_origin: Optional[str]) -> web.StreamResponse:
    if allow_origin:
        resp.headers["Access-Control-Allow-Origin"] = allow_origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "content-type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp

@routes.get("/")
async def health(_: web.Request):
    return web.Response(text="OK")

@routes.options("/apply")
async def preflight(request: web.Request):
    return _with_cors(web.Response(), _origin_ok(request))

@routes.post("/apply")
async def apply_color(request: web.Request):
    allow = _origin_ok(request)
    if not allow:
        return _with_cors(web.json_response({"ok": False, "msg": "origin not allowed"}, status=403), allow)

    try:
        body = await request.json()
        token = str(body.get("t", ""))
        hexv = str(body.get("hex", "")).lstrip("#").strip()

        if len(hexv) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hexv):
            return _with_cors(web.json_response({"ok": False, "msg": "invalid hex"}), allow)

        payload = signer.loads(token)  # {"g": guild_id, "u": user_id}
        gid = int(payload["g"])
        uid = int(payload["u"])
    except BadSignature:
        return _with_cors(web.json_response({"ok": False, "msg": "invalid token"}, status=400), allow)
    except Exception as e:
        return _with_cors(web.json_response({"ok": False, "msg": f"bad request: {e}"}, status=400), allow)

    guild = client.get_guild(gid)
    if not guild:
        return _with_cors(web.json_response({"ok": False, "msg": "guild not found"}, status=404), allow)

    # メンバー取得
    member = guild.get_member(uid)
    if not member:
        try:
            member = await guild.fetch_member(uid)
        except Exception:
            return _with_cors(web.json_response({"ok": False, "msg": "member not found"}, status=404), allow)

    # ボット権限チェック
    me: Optional[discord.Member] = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return _with_cors(web.json_response({"ok": False, "msg": "bot lacks Manage Roles"}), allow)

    # 重要ロール保護（ユーザーが保護ロールを持っていたら拒否する例）
    for r in member.roles:
        if r.id in PROTECTED_ROLE_IDS or r.name.lower() in PROTECTED_ROLE_NAMES:
            return _with_cors(web.json_response({"ok": False, "msg": "protected role user; deny"}), allow)

    try:
        rgb = int(hexv, 16)
        await _apply_member_color(member, rgb, me)
        return _with_cors(web.json_response({"ok": True, "msg": f"applied #{hexv.lower()}"}), allow)
    except PermissionError as e:
        return _with_cors(web.json_response({"ok": False, "msg": f"permission: {e}"}), allow)
    except Exception as e:
        return _with_cors(web.json_response({"ok": False, "msg": f"apply error: {e}"}), allow)

app = web.Application()
app.add_routes(routes)

# ========= Helpers =========
async def _apply_member_color(member: discord.Member, rgb_value: int, me: discord.Member):
    """個人カラー用ロールを作成/更新して付与"""
    guild = member.guild
    role_name = f"{PERSONAL_ROLE_PREFIX}{member.id}"
    role = discord.utils.get(guild.roles, name=role_name)

    # 役職位置（ボットより上のロールは編集不可）
    if role and me.top_role.position <= role.position:
        raise PermissionError("bot role must be higher than personal color role")

    if not role:
        role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(rgb_value),
            reason="Create personal color role",
            permissions=discord.Permissions.none()
        )
        # 可能ならボット直下へ
        try:
            await role.edit(position=max(me.top_role.position - 1, 1))
        except Exception:
            pass
        await member.add_roles(role, reason="Attach personal color role")
    else:
        await role.edit(colour=discord.Colour(rgb_value), reason="Update personal color role")
        if role not in member.roles:
            await member.add_roles(role, reason="Attach personal color role")

# ========= Slash Commands =========
@tree.command(name="color_web", description="外部ページで色を選び『Discordへ適用』できます（個人カラー）")
async def color_web(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user:
        await interaction.response.send_message("サーバーで実行してね。", ephemeral=True)
    else:
        token = signer.dumps({"g": interaction.guild.id, "u": interaction.user.id})
        base = ALLOW_ORIGINS[0] if ALLOW_ORIGINS else "https://example.com"
        url = f"{base}/?t={token}"  # りょーくんのページが /?t= 受け取り仕様
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="🎨 色を選ぶ（外部ページ）", url=url))
        await interaction.response.send_message(
            "外部ページで色を選んで『Discordへ適用』を押してね！\n"
            "※ ボットのロール位置は、付与したいロールより上に配置しておいてね。",
            view=view,
            ephemeral=True
        )

@tree.command(name="color_clear", description="自分の個人カラー（専用ロール）を削除します")
async def color_clear(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user:
        await interaction.response.send_message("サーバーで実行してね。", ephemeral=True)
        return
    role_name = f"{PERSONAL_ROLE_PREFIX}{interaction.user.id}"
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if not role:
        await interaction.response.send_message("個人カラーは見つからなかったよ。", ephemeral=True)
        return
    try:
        await role.delete(reason="Remove personal color role")
        await interaction.response.send_message("個人カラーを削除したよ。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"削除に失敗: {e}", ephemeral=True)

# ========= Lifecycle =========
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (id={client.user.id})")
    # 複数ギルドに即時同期（グローバルは反映に時間がかかるため、基本ギルド同期推奨）
    if GUILD_IDS:
        for gid in GUILD_IDS:
            try:
                await tree.sync(guild=discord.Object(id=gid))
                print(f"🔁 Synced commands to guild {gid}")
            except Exception as e:
                print(f"⚠️ Failed to sync guild {gid}: {e}")
    else:
        try:
            await tree.sync()
            print("🔁 Synced commands globally (may take minutes)")
        except Exception as e:
            print(f"⚠️ Global sync failed: {e}")

# ========= Entrypoint (Render用: PORTに明示バインド) =========
def main():
    async def start_servers():
        # Webサーバー起動（RenderのPort Binding検出に必須）
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"🌐 Web server started on :{PORT}")

        # Discord Bot 起動
        await client.start(TOKEN)

    asyncio.run(start_servers())

if __name__ == "__main__":
    main()

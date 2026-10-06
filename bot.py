import asyncio
import base64
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")  # pas obligatoire pour "login" et "export"
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID") or 0)
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = os.getenv("DB_PATH", "logs.db")

# Page de ton serveur sur Aternos (la page avec le gros bouton Démarrer)
SERVER_URL = os.getenv("ATERNOS_SERVER_URL", "https://aternos.org/server/")
# Dossier du profil Chrome (même dossier que login.py)
PROFILE_DIR = os.getenv("PROFILE_DIR", "aternos_profile")
# Navigateur : par défaut le Chromium intégré à Playwright. Optionnel : chemin de brave.exe / chrome.exe
BROWSER_PATH = os.getenv("BROWSER_PATH")
BROWSER_KW = {"executable_path": BROWSER_PATH} if BROWSER_PATH else {}  # {} = Chromium intégré à Playwright

# Sélecteurs de la page Aternos : si Aternos change son site, c'est ICI qu'on ajuste
SEL_START = "#start"
SEL_CONFIRM = "#confirm"
SEL_STATUS = ".statuslabel-label"


# ---------- Logs (SQLite) ----------
def db_init():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS start_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                date_utc TEXT NOT NULL,
                result TEXT NOT NULL
            )"""
        )


def db_add(user: discord.abc.User, result: str):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO start_log (user_id, username, date_utc, result) VALUES (?, ?, ?, ?)",
            (user.id, str(user), datetime.now(timezone.utc).isoformat(timespec="seconds"), result),
        )


def db_get(limit: int, user_id: int | None):
    with sqlite3.connect(DB_PATH) as db:
        if user_id:
            cur = db.execute(
                "SELECT user_id, username, date_utc, result FROM start_log "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            cur = db.execute(
                "SELECT user_id, username, date_utc, result FROM start_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return cur.fetchall()


# ---------- Navigateur Aternos ----------
class AternosBrowser:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.lock = asyncio.Lock()

    async def ensure(self):
        if self.ctx:
            return
        self.pw = await async_playwright().start()
        state_b64 = os.getenv("ATERNOS_STATE_B64")

        if state_b64:
            # Mode Railway : pas de profil, on réutilise la session exportée depuis ton PC
            data = json.loads(base64.b64decode(state_b64))
            browser = await self.pw.chromium.launch(
                headless=False,  # fenêtre dans un écran virtuel (xvfb) : moins détectable que headless
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            kwargs = {"storage_state": data["state"], "viewport": {"width": 1100, "height": 800}}
            if data.get("ua"):
                kwargs["user_agent"] = data["ua"]
            self.ctx = await browser.new_context(**kwargs)
        else:
            # Mode PC : navigateur avec profil persistant
            self.ctx = await self.pw.chromium.launch_persistent_context(
                PROFILE_DIR,
                **BROWSER_KW,
                headless=False,
                viewport={"width": 1100, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
        self.ctx.on("close", lambda *_: setattr(self, "ctx", None))

    async def _dismiss_popups(self, page):
        # Bandeau de consentement cookies (s'il apparaît)
        for sel in (".fc-cta-consent", "button:has-text('Consent')", "button:has-text('Accepter')"):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    break
            except Exception:
                pass

    async def _confirm_watch(self, page):
        """Après le clic sur Démarrer, Aternos peut demander une confirmation : on la clique."""
        try:
            for _ in range(200):  # ~10 minutes max
                if page.is_closed():
                    return
                try:
                    if await page.locator(SEL_CONFIRM).is_visible():
                        await page.locator(SEL_CONFIRM).click()
                        await asyncio.sleep(5)
                        break
                except Exception:
                    pass
                await asyncio.sleep(3)
        finally:
            if not page.is_closed():
                await page.close()

    async def start_if_needed(self) -> tuple[str, str]:
        """Retourne (résultat, statut). résultat : 'started', 'already' ou 'login'."""
        async with self.lock:
            await self.ensure()
            page = await self.ctx.new_page()
            try:
                await page.goto(SERVER_URL, wait_until="domcontentloaded")
                await self._dismiss_popups(page)

                if "/go" in page.url or "/login" in page.url:
                    await page.close()
                    return "login", ""

                try:
                    await page.wait_for_selector(SEL_STATUS, timeout=25000)
                except Exception:
                    raise RuntimeError(
                        "Page du serveur introuvable (Cloudflare ? sélecteur changé ? serveur non sélectionné ?)"
                    )

                status = (await page.locator(SEL_STATUS).first.inner_text()).strip()

                # Le bouton Démarrer n'est visible que si le serveur est éteint
                if await page.locator(SEL_START).is_visible():
                    await page.locator(SEL_START).click()
                    asyncio.create_task(self._confirm_watch(page))
                    return "started", status

                await page.close()
                return "already", status
            except Exception:
                if not page.is_closed():
                    await page.close()
                raise


aternos = AternosBrowser()



# ---------- Connexion à Aternos (à faire une seule fois : py bot.py login) ----------
async def login_mode():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            **BROWSER_KW,
            headless=False,
            viewport={"width": 1100, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://aternos.org/go/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"La page ne s'est pas chargée toute seule ({e}). Tape aternos.org/go/ dans la barre d'adresse.")
        print("1) Connecte-toi à Aternos dans la fenêtre (captcha compris).")
        print("2) Ouvre ton serveur : tu dois voir le bouton Démarrer.")
        input("3) Reviens ici et appuie sur Entrée pour enregistrer la session... ")
        await ctx.close()
        print("Session enregistrée. Lance maintenant : py bot.py")



# ---------- Export de la session pour Railway : py bot.py export ----------
async def export_mode():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, **BROWSER_KW, headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://aternos.org/server/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"Chargement impossible : {e}")
        if "/go" in page.url or "/login" in page.url:
            print("Tu n'es pas connecté à Aternos : lance d'abord  py bot.py login")
            await ctx.close()
            return
        ua = await page.evaluate("navigator.userAgent")
        state = await ctx.storage_state()
        await ctx.close()

    # On ne garde que ce qui concerne Aternos
    state["cookies"] = [c for c in state["cookies"] if "aternos" in c["domain"]]
    state["origins"] = [o for o in state["origins"] if "aternos" in o["origin"]]
    payload = json.dumps({"state": state, "ua": ua}).encode()
    with open("aternos_state.b64.txt", "w") as f:
        f.write(base64.b64encode(payload).decode())
    print("OK : aternos_state.b64.txt créé. Copie tout son contenu dans la variable ATERNOS_STATE_B64 sur Railway.")
    print("Ce fichier donne accès à ton compte Aternos : ne le partage pas, ne le mets pas sur GitHub.")


# ---------- Bot Discord ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        db_init()
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()


def is_mod(interaction: discord.Interaction) -> bool:
    return any(r.id == MOD_ROLE_ID for r in getattr(interaction.user, "roles", []))


@bot.tree.command(name="start", description="Lance le serveur Minecraft (Aternos)")
async def start(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        result, status = await aternos.start_if_needed()
    except Exception as e:
        db_add(interaction.user, "error")
        await interaction.followup.send(f"❌ Erreur : {e}")
        return

    db_add(interaction.user, result)  # chaque exécution est loguée
    if result == "login":
        await interaction.followup.send("⚠️ Le compte Aternos n'est plus connecté. Le propriétaire doit refaire `py bot.py login` puis `py bot.py export` et mettre à jour ATERNOS_STATE_B64.")
    elif result == "already":
        await interaction.followup.send(f"ℹ️ Le serveur est déjà lancé ou en cours de lancement (`{status}`).")
    else:
        await interaction.followup.send(f"🚀 {interaction.user.mention} a lancé le serveur ! Ça peut prendre quelques minutes (file d'attente Aternos).")


@bot.tree.command(name="logs", description="(Modo) Affiche qui a lancé le serveur")
@app_commands.describe(nombre="Nombre de lignes (max 50)", joueur="Filtrer sur un joueur")
async def logs(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 50] = 15,
               joueur: discord.Member | None = None):
    if not is_mod(interaction):
        await interaction.response.send_message("⛔ Commande réservée aux modos.", ephemeral=True)
        return

    rows = db_get(nombre, joueur.id if joueur else None)
    if not rows:
        await interaction.response.send_message("Aucun log pour l'instant.", ephemeral=True)
        return

    labels = {"started": "✅ lancé", "already": "ℹ️ déjà lancé", "login": "⚠️ non connecté", "error": "❌ erreur"}
    lines = []
    for user_id, username, date_utc, result in rows:
        ts = int(datetime.fromisoformat(date_utc).timestamp())
        lines.append(f"<t:{ts}:f> — <@{user_id}> ({username}) — {labels.get(result, result)}")

    embed = discord.Embed(title="Logs /start", description="\n".join(lines)[:4000], color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        asyncio.run(login_mode())
    elif len(sys.argv) > 1 and sys.argv[1] == "export":
        asyncio.run(export_mode())
    else:
        if not TOKEN or not MOD_ROLE_ID:
            sys.exit("Il manque DISCORD_TOKEN ou MOD_ROLE_ID : crée un fichier .env à côté de bot.py.")
        bot.run(TOKEN)